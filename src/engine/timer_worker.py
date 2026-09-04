"""Async deadline worker that turns quiet timers into durable lifecycle transitions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import time
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from src.contracts import CardChange, DeliveryIntent, EngineDecision, NormalizedEvent
from src.db.connection import Database

from .db_adapter import persist_decision
from .incident_machine import transition_state
from .timer_wheel import TimerTrigger, TimerWheel

POLL_INTERVAL_SECONDS = 0.1
logger = logging.getLogger(__name__)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _deadline_decision(
    tx: aiosqlite.Connection, trigger: TimerTrigger
) -> EngineDecision | None:
    """Build a ledger-backed QUIET_DEADLINE decision only for an acknowledged incident."""

    async with tx.execute(
        """
        SELECT
            i.incident_id, i.scope_key, i.stable_fingerprint, i.title, i.summary,
            i.severity, i.status, i.alert_count, i.last_alert_at,
            i.ewma_mean_gap, i.ewma_variance, i.gap_history_json, i.root_cause_hint,
            r.event_id AS source_event_id, r.service, r.alertname, r.severity_raw,
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
        WHERE i.incident_id = ?
        """,
        (str(trigger.incident_id),),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return None
    next_state = transition_state(row["status"], trigger.trigger)
    if next_state is None:
        return None

    labels = _as_json_dict(row["labels_json"])
    event_id = uuid4()
    fired_at = _parse_datetime(row["last_alert_at"])
    event = NormalizedEvent(
        event_id=event_id,
        fingerprint=f"lifecycle:{trigger.incident_id}:{trigger.quiet_at_ms}",
        source="generic",
        service=row["service"] or "pulsegraph-engine",
        alertname=row["alertname"] or "QuietDeadline",
        severity_raw=row["severity_raw"] or row["severity"],
        status="firing",
        labels={str(key): str(value) for key, value in labels.items()},
        message=f"Adaptive silence deadline reached for incident {trigger.incident_id}",
        fired_at=fired_at,
        raw_payload={
            "kind": "QUIET_DEADLINE",
            "incident_id": str(trigger.incident_id),
            "quiet_at_ms": trigger.quiet_at_ms,
            "source_event_id": row["source_event_id"],
        },
    )
    try:
        gap_history = [float(value) for value in json.loads(row["gap_history_json"])]
    except (TypeError, ValueError, json.JSONDecodeError):
        gap_history = []

    decision_payload = json.dumps(
        {
            "event_id": str(event_id),
            "incident_id": str(trigger.incident_id),
            "trigger": trigger.trigger,
            "quiet_at_ms": trigger.quiet_at_ms,
            "state": next_state,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    card_payload = json.dumps(
        {
            "incident_id": str(trigger.incident_id),
            "state": next_state,
            "quiet_at_ms": None,
            "card_change": "STATE_ACKNOWLEDGED_TO_QUIESCENT",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return EngineDecision(
        event=event,
        incident_id=trigger.incident_id,
        state=next_state,
        is_duplicate=True,
        severity_final=row["severity"],
        alert_count=int(row["alert_count"]),
        title=row["title"],
        summary=row["summary"],
        scope_key=row["scope_key"],
        stable_fingerprint=row["stable_fingerprint"],
        quiet_at_ms=None,
        ewma_mean_gap=float(row["ewma_mean_gap"]),
        ewma_variance=float(row["ewma_variance"]),
        gap_history=gap_history,
        card_changes=[CardChange(kind="STATE_ACKNOWLEDGED_TO_QUIESCENT")],
        decision_payload_json=decision_payload,
        delivery_intents=[
            DeliveryIntent(
                channel="slack",
                action="update",
                idempotency_key=(
                    f"quiet-deadline:{trigger.incident_id}:{trigger.quiet_at_ms}:slack"
                ),
                payload_json=card_payload,
            )
        ],
        root_cause_hint=row["root_cause_hint"],
    )


class TimerWorker:
    """One asyncio task that polls TimerWheel and commits due lifecycle events."""

    def __init__(
        self,
        database: Database,
        timer_wheel: TimerWheel,
        *,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        self._database = database
        self._timer_wheel = timer_wheel
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="pulsegraph-timer-worker")

    async def recover_persisted_deadlines(self, *, now_ms: int | None = None) -> int:
        """Rehydrate future ACKNOWLEDGED deadlines through the shared SQLite connection.

        The application calls this before starting the polling task, so the query and
        queue population form a startup boundary: no newly accepted alert can race a
        persisted deadline recovery.
        """

        recovery_now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        async with self._database.write_lock:
            tx = self._database.writer_conn
            if tx is None:
                raise RuntimeError("database writer connection is not available")
            async with tx.execute(
                """
                SELECT incident_id, quiet_at_ms
                FROM incidents
                WHERE status = 'ACKNOWLEDGED'
                  AND quiet_at_ms IS NOT NULL
                  AND quiet_at_ms > ?
                ORDER BY quiet_at_ms ASC
                """,
                (recovery_now_ms,),
            ) as cursor:
                rows = await cursor.fetchall()

        for row in rows:
            self._timer_wheel.schedule(UUID(row["incident_id"]), int(row["quiet_at_ms"]))

        recovered_count = len(rows)
        logger.info("timer_deadlines_recovered recovered_count=%s", recovered_count)
        return recovered_count

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _persist_trigger(self, trigger: TimerTrigger) -> bool:
        async with self._database.write_lock:
            tx = self._database.writer_conn
            if tx is None:
                raise RuntimeError("database writer connection is not available")
            await tx.execute("BEGIN IMMEDIATE")
            try:
                decision = await _deadline_decision(tx, trigger)
                if decision is None:
                    await tx.commit()
                    return False
                await persist_decision(tx, decision)
                await tx.commit()
                return True
            except Exception:
                await tx.rollback()
                raise

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            now_ms = int(time.time() * 1000)
            for trigger in self._timer_wheel.pop_due(now_ms):
                await self._persist_trigger(trigger)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval_seconds
                )
            except TimeoutError:
                continue


__all__ = ["POLL_INTERVAL_SECONDS", "TimerWorker"]
