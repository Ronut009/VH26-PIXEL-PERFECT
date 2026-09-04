"""Transaction-bound orchestration of dedupe, lifecycle, and adaptive silence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4

import aiosqlite

from src.config import settings
from src.contracts import CardChange, DeliveryIntent, EngineDecision, NormalizedEvent, Severity

from .adaptive_ewma import calculate_quiet_deadline
from .critical_bypass import classify_protected_critical
from .db_adapter import IncidentRecord, lookup_incident, persist_decision
from .dedupe import generate_fingerprint, is_exact_duplicate
from .incident_machine import transition_state
from src.graph.observe_incident import observe_incident
from src.graph.storm_grouping import (
    assign_group,
    redirect_member_deliveries,
    refresh_group_for_member,
)


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


def _iso_ms(epoch_ms: int) -> str:
    """Format an epoch millisecond value the way incident timestamps are stored."""

    moment = datetime.fromtimestamp(max(0, epoch_ms) / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def scope_key_for(event: NormalizedEvent) -> str:
    """The isolation boundary an event belongs to.

    Public because ingest authorisation has to answer "may this source write
    this scope" using exactly the scope the engine will later dedupe within.
    Two different derivations here would be a security hole, not a style issue.
    """

    environment = event.labels.get("environment", "default")
    cluster = event.labels.get("cluster", event.labels.get("namespace", "default"))
    return f"{environment}/{cluster}"


# Retained for internal callers written against the original private name.
_scope_key = scope_key_for


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


def _bounded_deadline(
    quiet_at_ms: int | None, first_alert_ms: int, now_ms: int
) -> int | None:
    """Stop an ongoing storm from deferring its own notification forever.

    Each new alert recomputes the quiet deadline as ``now + window``, so a
    service that keeps flapping keeps pushing its own delivery into the future
    and the incident is never announced at all. The adaptive window still
    decides *when* inside the budget; this decides that there is a budget.
    Once the ceiling is reached the incident fires with whatever it has, and
    later alerts keep updating the same card.
    """

    if quiet_at_ms is None:
        return None

    ceiling = first_alert_ms + settings.INCIDENT_MAX_BATCH_SPAN_MS
    if first_alert_ms <= 0 or quiet_at_ms <= ceiling:
        return quiet_at_ms
    # Never schedule a deadline in the past; a breached ceiling fires next tick.
    return max(ceiling, now_ms + 1)


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
            calculate_quiet_deadline(
                [], 0.0, event_time_ms, settings.QUIET_WINDOW_MAX_MS
            )
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
        calculate_quiet_deadline(
            list(record.gap_history),
            last_gap,
            event_time_ms,
            settings.QUIET_WINDOW_MAX_MS,
        )
        if state != "RESOLVED"
        else {"quiet_at_ms": None, "mean_gap": 0.0, "variance": 0.0}
    )
    quiet["quiet_at_ms"] = _bounded_deadline(
        quiet["quiet_at_ms"], record.first_alert_ms, event_time_ms
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

    # Correlate against a bounded neighbourhood, not every active incident in
    # the scope. This ran per alert and did an edge upsert per related
    # incident, so with A active incidents the work was O(A) per alert and the
    # edge set grew O(A**2) - all inside the write transaction holding the
    # single SQLite writer lock that ingest queues behind. The system got
    # slowest during exactly the storm it exists to absorb.
    #
    # Two bounds make it O(K): correlation is only meaningful over a recent
    # window, and past the most recent K neighbours the extra edges add cost
    # without adding evidence.
    window_start = _iso_ms(
        _event_time_ms(decision.event) - settings.CORRELATION_WINDOW_MS
    )
    placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
    async with transaction_obj.execute(
        f"""
        SELECT incident_id, stable_fingerprint
        FROM incidents
        WHERE scope_key = ? AND status IN ({placeholders})
          AND last_alert_at >= ?
        ORDER BY last_alert_at DESC
        LIMIT ?
        """,
        (
            decision.scope_key,
            *_ACTIVE_STATES,
            window_start,
            settings.CORRELATION_MAX_NEIGHBOURS,
        ),
    ) as cursor:
        rows = await cursor.fetchall()
    fingerprints = tuple(row["stable_fingerprint"] for row in rows)
    await observe_incident(transaction_obj, decision.incident_id, fingerprints)

    # Ranking deliberately does not happen here. A root cause is an enrichment,
    # not a transactional invariant: nothing about durably recording this alert
    # or delivering its notification depends on knowing what caused it. The
    # observation round above marks the scope dirty and RootCauseWorker ranks it
    # off the write path, debounced - so a storm of alerts costs a handful of
    # ranking passes instead of one per alert. Delivery payloads render from
    # live incident state at send time, so a hint that lands after a card was
    # queued still reaches it.

    # Correlation stops at annotation unless something acts on it. If this
    # incident is now strongly tied to others, they become one storm and only
    # the anchor keeps a card - so a three-service cascade pages once, not
    # three times, and the consequences stop competing with their own cause.
    incident_id = str(decision.incident_id)
    assignment = await assign_group(transaction_obj, incident_id, decision.scope_key)
    if assignment is None:
        # No new correlation, but this incident's own state just changed, so a
        # storm it already belongs to needs its derived facts recomputed.
        assignment = await refresh_group_for_member(transaction_obj, incident_id)
    if (
        assignment is not None
        and assignment.is_multi_member
        and assignment.anchor_incident_id != incident_id
    ):
        await redirect_member_deliveries(
            transaction_obj, incident_id, assignment.anchor_incident_id
        )

    return decision


__all__ = ["persist_and_observe", "process_event", "scope_key_for"]
