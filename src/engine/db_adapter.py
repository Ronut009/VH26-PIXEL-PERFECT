"""SQLite reads and writes owned by the DbWriter transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID

import aiosqlite

from src.contracts import EngineDecision
from src.db.hashchain import compute_row_hash, next_seq_and_prev_hash


@dataclass(frozen=True)
class IncidentRecord:
    incident_id: UUID
    state: str
    last_event: dict[str, Any]
    last_seen_ms: int
    gap_history: tuple[float, ...]
    alert_count: int


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _epoch_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _canonical_payload(decision: EngineDecision) -> str:
    payload = {
        "event": decision.event.model_dump(mode="json"),
        "decision": json.loads(decision.decision_payload_json),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def lookup_incident(
    tx: aiosqlite.Connection, fingerprint: str, scope_key: str
) -> IncidentRecord | None:
    """Find the most recently updated incident for one stable identity and scope."""

    async with tx.execute(
        """
        SELECT
            i.incident_id,
            i.status,
            i.last_alert_at,
            i.gap_history_json,
            i.alert_count,
            r.service,
            r.alertname,
            r.severity_raw,
            r.status AS event_status,
            r.labels_json
        FROM incidents AS i
        LEFT JOIN raw_events AS r
          ON r.event_id = (
              SELECT latest.event_id
              FROM raw_events AS latest
              WHERE latest.incident_id = i.incident_id
              ORDER BY latest.seq DESC
              LIMIT 1
          )
        WHERE i.stable_fingerprint = ?
          AND i.scope_key = ?
        ORDER BY i.updated_at DESC
        LIMIT 1
        """,
        (fingerprint, scope_key),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return None

    try:
        gap_history = tuple(float(value) for value in json.loads(row["gap_history_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        gap_history = ()

    labels = json.loads(row["labels_json"]) if row["labels_json"] else {}
    last_event = {
        "scope_key": scope_key,
        "service": row["service"] or "",
        "alertname": row["alertname"] or "",
        "severity_raw": row["severity_raw"] or "",
        "status": row["event_status"] or "",
        "labels": labels,
    }
    return IncidentRecord(
        incident_id=UUID(row["incident_id"]),
        state=row["status"],
        last_event=last_event,
        last_seen_ms=_epoch_ms(row["last_alert_at"]),
        gap_history=gap_history,
        alert_count=int(row["alert_count"]),
    )


async def persist_decision(tx: aiosqlite.Connection, decision: EngineDecision) -> None:
    """Persist one decision without beginning, committing, or rolling back a transaction."""

    event = decision.event
    event_time = _iso(event.fired_at)
    now = _iso(datetime.now(timezone.utc))
    route = "pagerduty" if decision.severity_final == "critical" else "slack"

    await tx.execute(
        """
        INSERT INTO incidents (
            incident_id, scope_key, stable_fingerprint, title, summary, severity,
            status, alert_count, first_alert_at, last_alert_at, ewma_rate,
            quiet_at_ms, ewma_mean_gap, ewma_variance, gap_history_json,
            route_decision, root_cause_hint, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(incident_id) DO UPDATE SET
            title = excluded.title,
            summary = COALESCE(excluded.summary, incidents.summary),
            severity = excluded.severity,
            status = excluded.status,
            alert_count = excluded.alert_count,
            last_alert_at = excluded.last_alert_at,
            ewma_rate = excluded.ewma_rate,
            quiet_at_ms = excluded.quiet_at_ms,
            ewma_mean_gap = excluded.ewma_mean_gap,
            ewma_variance = excluded.ewma_variance,
            gap_history_json = excluded.gap_history_json,
            route_decision = excluded.route_decision,
            root_cause_hint = COALESCE(excluded.root_cause_hint, incidents.root_cause_hint),
            updated_at = excluded.updated_at
        """,
        (
            str(decision.incident_id),
            decision.scope_key,
            decision.stable_fingerprint,
            decision.title,
            decision.summary,
            decision.severity_final,
            decision.state,
            decision.alert_count,
            event_time,
            event_time,
            decision.ewma_mean_gap,
            decision.quiet_at_ms,
            decision.ewma_mean_gap,
            decision.ewma_variance,
            json.dumps(decision.gap_history, separators=(",", ":")),
            route,
            decision.root_cause_hint,
            now,
            now,
        ),
    )

    seq, prev_hash = await next_seq_and_prev_hash(tx)
    canonical_payload = _canonical_payload(decision)
    row_hash = compute_row_hash(prev_hash, canonical_payload)
    await tx.execute(
        """
        INSERT INTO raw_events (
            event_id, seq, fingerprint, stable_fingerprint, scope_key, source,
            service, alertname, severity_raw, status, labels_json, message,
            fired_at, raw_payload, prev_hash, row_hash, incident_id,
            is_duplicate, bypassed, bypass_reason, decision_payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(event.event_id),
            seq,
            event.fingerprint,
            decision.stable_fingerprint,
            decision.scope_key,
            event.source,
            event.service,
            event.alertname,
            event.severity_raw,
            event.status,
            json.dumps(event.labels, sort_keys=True, separators=(",", ":")),
            event.message,
            event_time,
            json.dumps(event.raw_payload, sort_keys=True, separators=(",", ":")),
            prev_hash,
            row_hash,
            str(decision.incident_id),
            int(decision.is_duplicate),
            int(decision.is_critical_bypass),
            decision.bypass_reason,
            decision.decision_payload_json,
        ),
    )

    for intent in decision.delivery_intents:
        await tx.execute(
            """
            INSERT INTO delivery_intents (
                incident_id, event_id, channel, action, idempotency_key, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                str(decision.incident_id),
                str(event.event_id),
                intent.channel,
                intent.action,
                intent.idempotency_key,
                intent.payload_json,
            ),
        )
        await tx.execute(
            """
            INSERT INTO outbox (
                incident_id, channel, action, payload_json, status, next_attempt_at
            ) VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                str(decision.incident_id),
                intent.channel,
                "create" if intent.action == "trigger" else intent.action,
                intent.payload_json,
                now,
            ),
        )


__all__ = ["IncidentRecord", "lookup_incident", "persist_decision"]
