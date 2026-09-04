import asyncio
import json
from datetime import datetime, timedelta, timezone

from src.config import settings
from src.db.connection import Database
from src.outbox import pagerduty, slack
from src.utils.logging import get_logger

logger = get_logger(__name__)

_DISPATCHERS = {
    "slack": slack.send,
    "pagerduty": pagerduty.send,
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _utcnow_iso() -> str:
    return _iso(datetime.now(timezone.utc))


def _exponential_backoff(attempts: int) -> timedelta:
    seconds = min(2**attempts, 300)
    return timedelta(seconds=seconds)


class OutboxWorker:
    def __init__(self, db: Database):
        self.db = db
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        poll_interval = settings.OUTBOX_POLL_INTERVAL_MS / 1000
        while True:
            try:
                await self._poll_once()
            except Exception:
                logger.exception("outbox_poll_error")
            await asyncio.sleep(poll_interval)

    async def _poll_once(self) -> None:
        now = _utcnow_iso()
        conn = self.db.writer_conn
        if conn is None:
            return

        async with self.db.write_lock:
            async with conn.execute(
                """
                SELECT outbox_id, incident_id, channel, action, payload_json, attempts, external_ref
                FROM outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY outbox_id
                LIMIT 10
                """,
                (now,),
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]

        for row in rows:
            await self._dispatch(row)

    async def _dispatch(self, row: dict) -> None:
        outbox_id = row["outbox_id"]
        channel = row["channel"]
        action = row["action"]
        payload = json.loads(row["payload_json"])
        external_ref = row["external_ref"]

        if channel == "email":
            await self._mark_sent(outbox_id, external_ref=None)
            logger.info(
                "email_stub_sent",
                outbox_id=outbox_id,
                incident_id=row["incident_id"],
                title=payload.get("title"),
            )
            return

        dispatcher = _DISPATCHERS.get(channel)
        if dispatcher is None:
            await self._mark_failed(outbox_id, row["attempts"], f"unknown channel: {channel}")
            return

        try:
            new_ref = await dispatcher(action, payload, external_ref)
            await self._mark_sent(outbox_id, external_ref=new_ref)
            logger.info(
                "outbox_delivered",
                outbox_id=outbox_id,
                incident_id=row["incident_id"],
                channel=channel,
                action=action,
            )
        except Exception as exc:
            await self._mark_failed(outbox_id, row["attempts"], str(exc))
            logger.warning(
                "outbox_delivery_failed",
                outbox_id=outbox_id,
                incident_id=row["incident_id"],
                channel=channel,
                action=action,
                error=str(exc),
            )

    async def _mark_sent(self, outbox_id: int, external_ref: str | None) -> None:
        conn = self.db.writer_conn
        now = _utcnow_iso()
        async with self.db.write_lock:
            await conn.execute(
                "UPDATE outbox SET status = 'sent', sent_at = ?, external_ref = COALESCE(?, external_ref) WHERE outbox_id = ?",
                (now, external_ref, outbox_id),
            )
            await conn.commit()

    async def _mark_failed(self, outbox_id: int, attempts: int, error: str) -> None:
        conn = self.db.writer_conn
        new_attempts = attempts + 1
        status = "dead" if new_attempts >= settings.OUTBOX_MAX_ATTEMPTS else "pending"
        next_attempt_at = _iso(datetime.now(timezone.utc) + _exponential_backoff(new_attempts))

        async with self.db.write_lock:
            await conn.execute(
                """
                UPDATE outbox
                SET attempts = ?, status = ?, next_attempt_at = ?, last_error = ?
                WHERE outbox_id = ?
                """,
                (new_attempts, status, next_attempt_at, error[:1000], outbox_id),
            )
            await conn.commit()
