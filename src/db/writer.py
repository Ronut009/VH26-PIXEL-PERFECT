import json
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import uuid4

import aiosqlite

from src.contracts import GraphUpdate, IncidentDecision, NormalizedEvent
from src.db.hashchain import canonical_json, compute_row_hash, next_seq_and_prev_hash
from src.ingest.policy import critical_bypass
from src.utils.logging import get_logger

logger = get_logger(__name__)

ProcessIncidentFn = Callable[[aiosqlite.Connection, NormalizedEvent], Awaitable[IncidentDecision]]
UpdateGraphFn = Callable[[aiosqlite.Connection, NormalizedEvent, IncidentDecision], Awaitable[GraphUpdate]]

_CHANNEL_BY_SEVERITY = {
    "critical": "pagerduty",
    "high": "slack",
    "medium": "slack",
    "low": "email",
}


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _utcnow_iso() -> str:
    return _iso(datetime.now(timezone.utc))


class DbWriter:
    """Transactional orchestrator: raw_events (hash-chained) + incidents + outbox,
    all inside a single BEGIN IMMEDIATE transaction.

    process_incident_fn / update_graph_fn are injected so this can run against
    stub_process_incident / stub_update_graph today and swap to Vansh's and
    Anish's real implementations later with no changes here.
    """

    def __init__(self, process_incident_fn: ProcessIncidentFn, update_graph_fn: UpdateGraphFn):
        self.process_incident_fn = process_incident_fn
        self.update_graph_fn = update_graph_fn

    async def process_event(self, db_conn: aiosqlite.Connection, event: NormalizedEvent) -> dict:
        await db_conn.execute("BEGIN IMMEDIATE")
        try:
            seq, prev_hash = await next_seq_and_prev_hash(db_conn)
            row_hash = compute_row_hash(prev_hash, canonical_json(event))
            bypassed = critical_bypass(event)

            if bypassed:
                incident_id, decision = await self._handle_bypass(db_conn, event)
            else:
                decision = await self.process_incident_fn(db_conn, event)
                graph = await self.update_graph_fn(db_conn, event, decision)
                await self._upsert_incident(db_conn, event, decision, graph)
                incident_id = decision.incident_id

                if not decision.is_duplicate and decision.status in ("new", "active"):
                    await self._enqueue_outbox(db_conn, event, decision, graph)

            await db_conn.execute(
                """
                INSERT INTO raw_events (
                    event_id, seq, fingerprint, source, service, alertname,
                    severity_raw, status, labels_json, message, fired_at,
                    raw_payload, prev_hash, row_hash, incident_id, is_duplicate, bypassed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.event_id),
                    seq,
                    event.fingerprint,
                    event.source,
                    event.service,
                    event.alertname,
                    event.severity_raw,
                    event.status,
                    json.dumps(event.labels, ensure_ascii=False),
                    event.message,
                    _iso(event.fired_at),
                    json.dumps(event.raw_payload, ensure_ascii=False),
                    prev_hash,
                    row_hash,
                    str(incident_id),
                    int(decision.is_duplicate),
                    int(bypassed),
                ),
            )

            await db_conn.commit()
        except Exception:
            await db_conn.rollback()
            raise

        logger.info(
            "event_processed",
            event_id=str(event.event_id),
            incident_id=str(incident_id),
            seq=seq,
            bypassed=bypassed,
            is_duplicate=decision.is_duplicate,
            status=decision.status,
        )

        return {
            "event_id": str(event.event_id),
            "incident_id": str(incident_id),
            "seq": seq,
            "row_hash": row_hash,
            "bypassed": bypassed,
            "is_duplicate": decision.is_duplicate,
            "status": decision.status,
        }

    async def _handle_bypass(
        self, db_conn: aiosqlite.Connection, event: NormalizedEvent
    ) -> tuple:
        incident_id = uuid4()
        now = _utcnow_iso()
        title = f"{event.service} — {event.alertname}"

        decision = IncidentDecision(
            incident_id=incident_id,
            status="new",
            is_duplicate=False,
            severity_final="critical",
            alert_count=1,
            ewma_rate=0.0,
            title=title,
            summary=event.message,
        )

        await db_conn.execute(
            """
            INSERT INTO incidents (
                incident_id, title, summary, severity, status, alert_count,
                first_alert_at, last_alert_at, ewma_rate, route_decision,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(incident_id),
                title,
                event.message,
                "critical",
                "new",
                1,
                _iso(event.fired_at),
                _iso(event.fired_at),
                0.0,
                "pagerduty",
                now,
                now,
            ),
        )

        payload = {
            "incident_id": str(incident_id),
            "title": title,
            "summary": event.message,
            "severity": "critical",
            "status": "new",
            "alert_count": 1,
            "service": event.service,
            "ewma_rate": 0.0,
            "root_cause_hint": None,
            "timestamp": now,
        }
        await db_conn.execute(
            """
            INSERT INTO outbox (incident_id, channel, action, payload_json, status, next_attempt_at)
            VALUES (?, 'pagerduty', 'create', ?, 'pending', ?)
            """,
            (str(incident_id), json.dumps(payload, ensure_ascii=False), now),
        )

        return incident_id, decision

    async def _upsert_incident(
        self,
        db_conn: aiosqlite.Connection,
        event: NormalizedEvent,
        decision: IncidentDecision,
        graph: GraphUpdate,
    ) -> None:
        now = _utcnow_iso()
        channel = _CHANNEL_BY_SEVERITY.get(decision.severity_final, "slack")

        async with db_conn.execute(
            "SELECT incident_id FROM incidents WHERE incident_id = ?",
            (str(decision.incident_id),),
        ) as cursor:
            existing = await cursor.fetchone()

        if existing is None:
            await db_conn.execute(
                """
                INSERT INTO incidents (
                    incident_id, title, summary, severity, status, alert_count,
                    first_alert_at, last_alert_at, ewma_rate, route_decision,
                    root_cause_hint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(decision.incident_id),
                    decision.title,
                    decision.summary,
                    decision.severity_final,
                    decision.status,
                    decision.alert_count,
                    _iso(event.fired_at),
                    _iso(event.fired_at),
                    decision.ewma_rate,
                    channel,
                    graph.root_cause_hint,
                    now,
                    now,
                ),
            )
        else:
            await db_conn.execute(
                """
                UPDATE incidents SET
                    alert_count = ?,
                    last_alert_at = ?,
                    ewma_rate = ?,
                    status = ?,
                    severity = ?,
                    summary = COALESCE(?, summary),
                    route_decision = ?,
                    root_cause_hint = COALESCE(?, root_cause_hint),
                    updated_at = ?
                WHERE incident_id = ?
                """,
                (
                    decision.alert_count,
                    _iso(event.fired_at),
                    decision.ewma_rate,
                    decision.status,
                    decision.severity_final,
                    decision.summary,
                    channel,
                    graph.root_cause_hint,
                    now,
                    str(decision.incident_id),
                ),
            )

    async def _enqueue_outbox(
        self,
        db_conn: aiosqlite.Connection,
        event: NormalizedEvent,
        decision: IncidentDecision,
        graph: GraphUpdate,
    ) -> None:
        channel = _CHANNEL_BY_SEVERITY.get(decision.severity_final, "slack")
        action = "create" if decision.status == "new" else "update"

        payload = {
            "incident_id": str(decision.incident_id),
            "title": decision.title,
            "summary": decision.summary,
            "severity": decision.severity_final,
            "status": decision.status,
            "alert_count": decision.alert_count,
            "ewma_rate": decision.ewma_rate,
            "root_cause_hint": graph.root_cause_hint,
            "service": event.service,
            "timestamp": _utcnow_iso(),
        }

        await db_conn.execute(
            """
            INSERT INTO outbox (incident_id, channel, action, payload_json, status, next_attempt_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                str(decision.incident_id),
                channel,
                action,
                json.dumps(payload, ensure_ascii=False),
                _utcnow_iso(),
            ),
        )
