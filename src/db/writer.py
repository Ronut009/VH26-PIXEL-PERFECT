"""The single SQLite writer for engine decisions and hash-chained audit records."""

from __future__ import annotations

import aiosqlite
import logging

from src.contracts import NormalizedEvent
from src.engine.db_adapter import persist_decision
from src.engine.process_event import process_event as process_engine_event

logger = logging.getLogger(__name__)


class DbWriter:
    """Own one BEGIN IMMEDIATE transaction per normalized alert event."""

    async def process_event(
        self, db_conn: aiosqlite.Connection, event: NormalizedEvent
    ) -> dict[str, str | int | bool | None]:
        await db_conn.execute("BEGIN IMMEDIATE")
        try:
            decision = await process_engine_event(db_conn, event)
            await persist_decision(db_conn, decision)
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
        }
