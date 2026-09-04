"""Transaction-bound orchestration of dedupe, lifecycle, and adaptive silence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4

import aiosqlite

from src.contracts import CardChange, DeliveryIntent, EngineDecision, NormalizedEvent, Severity

from .adaptive_ewma import calculate_quiet_deadline
from .critical_bypass import classify_protected_critical
from .db_adapter import IncidentRecord, lookup_incident, persist_decision
from .dedupe import generate_fingerprint, is_exact_duplicate
from .incident_machine import transition_state
from src.graph.observe_incident import observe_incident
from src.graph.root_cause_ranker import rank_root_cause


_SEVERITY_MAP: dict[str, Severity] = {
    "critical": "critical",
    "error": "high",
    "high": "high",
    "warning": "medium",
    "medium": "medium",
    "info": "low",
    "low": "low",
}
_ACTIVE_STATES = ("OPEN", "ACKNOWLEDGED", "QUIESCENT")


def _event_time_ms(event: NormalizedEvent) -> int:
    fired_at: datetime = event.fired_at
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)
    return int(fired_at.timestamp() * 1000)


def _scope_key(event: NormalizedEvent) -> str:
    environment = event.labels.get("environment", "default")
    cluster = event.labels.get("cluster", event.labels.get("namespace", "default"))
    return f"{environment}/{cluster}"


def _event_payload(event: NormalizedEvent, scope_key: str) -> dict[str, Any]:
    return {
        "scope_key": scope_key,
        "service": event.service,
        "alertname": event.alertname,
        "severity_raw": event.severity_raw,
        "status": event.status,
        "labels": dict(event.labels),
    }


def _severity(event: NormalizedEvent) -> Severity:
    return _SEVERITY_MAP.get(event.severity_raw.lower(), "medium")


def _payload_json(
    *,
    event: NormalizedEvent,
    incident_id: str,
    state: str,
    is_duplicate: bool,
    bypass_reason: str | None,
) -> str:
    return json.dumps(
        {
            "event_id": str(event.event_id),
            "incident_id": incident_id,
            "state": state,
            "is_duplicate": is_duplicate,
            "bypass_reason": bypass_reason,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _slack_intent(
    event: NormalizedEvent, incident_id: str, action: str, payload: dict[str, Any]
) -> DeliveryIntent:
    return DeliveryIntent(
        channel="slack",
        action=action,  # type: ignore[arg-type]
        idempotency_key=f"card:{event.event_id}:{incident_id}:{action}",
        payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def _decision(
    *,
    event: NormalizedEvent,
    incident_id: str,
    state: str,
    is_duplicate: bool,
    scope_key: str,
    stable_fingerprint: str,
    quiet_at_ms: int | None,
    mean_gap: float,
    variance: float,
    gap_history: list[float],
    card_changes: list[str],
    alert_count: int,
    is_critical_bypass: bool = False,
    bypass_reason: str | None = None,
    delivery_intents: list[DeliveryIntent] | None = None,
) -> EngineDecision:
    return EngineDecision(
        event=event,
        incident_id=incident_id,
        state=state,  # type: ignore[arg-type]
        is_duplicate=is_duplicate,
        severity_final="critical" if is_critical_bypass else _severity(event),
        alert_count=alert_count,
        title=f"{event.service} — {event.alertname}",
        summary=event.message,
        scope_key=scope_key,
        stable_fingerprint=stable_fingerprint,
        quiet_at_ms=quiet_at_ms,
        ewma_mean_gap=mean_gap,
        ewma_variance=variance,
        gap_history=gap_history,
        card_changes=[CardChange(kind=change) for change in card_changes],
        is_critical_bypass=is_critical_bypass,
        bypass_reason=bypass_reason,
        decision_payload_json=_payload_json(
            event=event,
            incident_id=incident_id,
            state=state,
            is_duplicate=is_duplicate,
            bypass_reason=bypass_reason,
        ),
        delivery_intents=delivery_intents or [],
    )


def _new_incident_state(event: NormalizedEvent) -> str:
    if event.status == "resolved":
        return "RESOLVED"
    return transition_state("OPEN", "ACKNOWLEDGE") or "OPEN"


def _next_state(record: IncidentRecord, event: NormalizedEvent) -> str:
    if event.status == "resolved":
        return transition_state(record.state, "RESOLVE") or record.state
    if record.state == "RESOLVED":
        reopened = transition_state(record.state, "REOPEN") or record.state
        return transition_state(reopened, "ACKNOWLEDGE") or reopened
    if record.state == "OPEN":
        return transition_state(record.state, "ACKNOWLEDGE") or record.state
    return record.state


async def process_event(
    transaction_obj: aiosqlite.Connection, normalized_event: NormalizedEvent
) -> EngineDecision:
    """Read through the active transaction and return data for atomic persistence."""

    scope_key = _scope_key(normalized_event)
    event_payload = _event_payload(normalized_event, scope_key)
    stable_fingerprint = generate_fingerprint(event_payload)
    bypass = classify_protected_critical(normalized_event)

    if bypass.should_bypass:
        incident_id = str(uuid4())
        payload = {
            "incident_id": incident_id,
            "event_id": str(normalized_event.event_id),
            "service": normalized_event.service,
            "severity": "critical",
            "bypass_reason": bypass.reason,
        }
        intents = [
            DeliveryIntent(
                channel="pagerduty",
                action="trigger",
                idempotency_key=f"critical:{normalized_event.event_id}:{incident_id}:pagerduty",
                payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
            _slack_intent(normalized_event, incident_id, "create", payload),
        ]
        return _decision(
            event=normalized_event,
            incident_id=incident_id,
            state="ACKNOWLEDGED",
            is_duplicate=False,
            scope_key=scope_key,
            stable_fingerprint=stable_fingerprint,
            quiet_at_ms=None,
            mean_gap=0.0,
            variance=0.0,
            gap_history=[],
            card_changes=["CRITICAL_BYPASS", "STATE_OPEN_TO_ACKNOWLEDGED"],
            alert_count=1,
            is_critical_bypass=True,
            bypass_reason=bypass.reason,
            delivery_intents=intents,
        )

    record = await lookup_incident(transaction_obj, stable_fingerprint, scope_key)
    event_time_ms = _event_time_ms(normalized_event)

    if record is None:
        state = _new_incident_state(normalized_event)
        quiet = (
            calculate_quiet_deadline([], 0.0, event_time_ms)
            if state != "RESOLVED"
            else {"quiet_at_ms": None, "mean_gap": 0.0, "variance": 0.0}
        )
        incident_id = str(uuid4())
        payload = {
            "incident_id": incident_id,
            "state": state,
            "alert_count": 1,
            "service": normalized_event.service,
        }
        return _decision(
            event=normalized_event,
            incident_id=incident_id,
            state=state,
            is_duplicate=False,
            scope_key=scope_key,
            stable_fingerprint=stable_fingerprint,
            quiet_at_ms=quiet["quiet_at_ms"],
            mean_gap=float(quiet["mean_gap"]),
            variance=float(quiet["variance"]),
            gap_history=[],
            card_changes=["INCIDENT_OPENED", "STATE_OPEN_TO_ACKNOWLEDGED"],
            alert_count=1,
            delivery_intents=[_slack_intent(normalized_event, incident_id, "create", payload)],
        )

    is_duplicate = is_exact_duplicate(event_payload, record.last_event, scope_key)
    if not is_duplicate:
        raise RuntimeError("stable fingerprint lookup returned a non-identical incident")

    state = _next_state(record, normalized_event)
    last_gap = max(0.0, float(event_time_ms - record.last_seen_ms))
    quiet = (
        calculate_quiet_deadline(list(record.gap_history), last_gap, event_time_ms)
        if state != "RESOLVED"
        else {"quiet_at_ms": None, "mean_gap": 0.0, "variance": 0.0}
    )
    incident_id = str(record.incident_id)
    payload = {
        "incident_id": incident_id,
        "state": state,
        "alert_count": record.alert_count + 1,
        "service": normalized_event.service,
    }
    changes = ["DUPLICATE_COALESCED", "QUIET_DEADLINE_UPDATED"]
    if state != record.state:
        changes.append(f"STATE_{record.state}_TO_{state}")
    return _decision(
        event=normalized_event,
        incident_id=incident_id,
        state=state,
        is_duplicate=True,
        scope_key=scope_key,
        stable_fingerprint=stable_fingerprint,
        quiet_at_ms=quiet["quiet_at_ms"],
        mean_gap=float(quiet["mean_gap"]),
        variance=float(quiet["variance"]),
        gap_history=[*record.gap_history, last_gap],
        card_changes=changes,
        alert_count=record.alert_count + 1,
        delivery_intents=[_slack_intent(normalized_event, incident_id, "update", payload)],
    )


async def persist_and_observe(
    transaction_obj: aiosqlite.Connection, decision: EngineDecision
) -> EngineDecision:
    """Persist an engine decision, then derive graph evidence in the same transaction."""

    await persist_decision(transaction_obj, decision)
    if decision.is_critical_bypass:
        return decision

    placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
    async with transaction_obj.execute(
        f"""
        SELECT stable_fingerprint
        FROM incidents
        WHERE scope_key = ? AND status IN ({placeholders})
        """,
        (decision.scope_key, *_ACTIVE_STATES),
    ) as cursor:
        rows = await cursor.fetchall()
    fingerprints = tuple(row["stable_fingerprint"] for row in rows)
    await observe_incident(transaction_obj, decision.incident_id, fingerprints)

    decision.root_cause_hint = await rank_root_cause(transaction_obj)
    if decision.root_cause_hint is not None:
        await transaction_obj.execute(
            "UPDATE incidents SET root_cause_hint = ? WHERE incident_id = ?",
            (decision.root_cause_hint, str(decision.incident_id)),
        )
    return decision


__all__ = ["persist_and_observe", "process_event"]
