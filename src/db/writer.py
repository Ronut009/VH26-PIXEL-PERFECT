"""The single SQLite writer for engine decisions and hash-chained audit records."""

from __future__ import annotations

import aiosqlite
import logging

from src.contracts import NormalizedEvent
from src.engine.process_event import persist_and_observe, process_event as process_engine_event
from src.engine.timer_wheel import TimerWheel

logger = logging.getLogger(__name__)


class DbWriter:
    """Own one BEGIN IMMEDIATE transaction per normalized alert event."""

    def __init__(self, timer_wheel: TimerWheel | None = None) -> None:
        self._timer_wheel = timer_wheel

    async def process_event(
        self, db_conn: aiosqlite.Connection, event: NormalizedEvent
    ) -> dict[str, str | int | bool | None]:
        await db_conn.execute("BEGIN IMMEDIATE")
        try:
            decision = await process_engine_event(db_conn, event)
            decision = await persist_and_observe(db_conn, decision)
            async with db_conn.execute(
                "SELECT seq, row_hash FROM raw_events WHERE event_id = ?",
                (str(event.event_id),),
            ) as cursor:
                raw_event = await cursor.fetchone()
            if raw_event is None:
                raise RuntimeError("persist_decision did not append the raw event")
            await db_conn.commit()
        except Exception:
            await db_conn.rollback()
            raise

        if self._timer_wheel is not None and decision.quiet_at_ms is not None:
            self._timer_wheel.schedule(decision.incident_id, decision.quiet_at_ms)

        logger.info(
            "event_processed event_id=%s incident_id=%s seq=%s bypassed=%s duplicate=%s state=%s",
            event.event_id,
            decision.incident_id,
            raw_event["seq"],
            decision.is_critical_bypass,
            decision.is_duplicate,
            decision.state,
        )
        return {
            "event_id": str(event.event_id),
            "incident_id": str(decision.incident_id),
            "seq": raw_event["seq"],
            "row_hash": raw_event["row_hash"],
            "bypassed": decision.is_critical_bypass,
            "is_duplicate": decision.is_duplicate,
            "status": decision.state,
            "quiet_at_ms": decision.quiet_at_ms,
            "root_cause_hint": decision.root_cause_hint,
        }
