"""Drains the transactional outbox, and survives a channel being down.

The queue is deliberately the only path to the outside world: "decide to
notify" happens inside the write transaction that changed the incident, and
"actually notify" happens here. That split is what makes a Slack outage a
delivery problem rather than a data-loss problem.

What the worker adds on top of the queue:

* It asks ``channel_health`` whether a channel is up before dispatching, so a
  known-dead channel never consumes any row's retry budget.
* It classifies failures, so a bad payload is dead-lettered while an outage
  parks the whole channel.
* It fails urgent traffic over to an independent provider the moment an outage
  is declared, instead of leaving criticals in a queue nobody is watching.
* It coalesces and re-renders the backlog on recovery, so coming back up posts
  current state once per incident rather than replaying history.
"""

import asyncio
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from src.config import settings
from src.db.connection import Database
from src.outbox import email, pagerduty, recovery, slack
from src.outbox.channel_health import HALF_OPEN, BreakerConfig, ChannelHealthStore
from src.outbox.failure_policy import FailureKind, classify
from src.outbox.routing import is_failover_worthy, select_failover
from src.utils.logging import get_logger

logger = get_logger(__name__)

_DISPATCHERS = {
    "slack": slack.send,
    "pagerduty": pagerduty.send,
    "email": email.send,
}

_PROBES = {
    "slack": slack.probe,
    "pagerduty": pagerduty.probe,
    "email": email.probe,
}

# Channels the worker knows about, so a breaker can be evaluated for each even
# before any row for it has ever failed.
_KNOWN_CHANNELS = ("slack", "pagerduty", "email")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _utcnow_iso() -> str:
    return _iso(datetime.now(timezone.utc))


def _exponential_backoff(attempts: int) -> timedelta:
    seconds = min(2**attempts, 300)
    return timedelta(seconds=seconds)


class OutboxWorker:
    def __init__(
        self,
        db: Database,
        breaker_config: BreakerConfig | None = None,
        worker_id: str | None = None,
    ):
        self.db = db
        self.health = ChannelHealthStore(breaker_config)
        # Identifies this worker's claims. Host and pid make an abandoned lease
        # traceable to the process that died holding it.
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
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
                await self._probe_open_channels()
                await self._poll_once()
            except Exception:
                logger.exception("outbox_poll_error")
            await asyncio.sleep(poll_interval)

    # ── recovery detection ────────────────────────────────────────────────

    async def _probe_open_channels(self) -> None:
        """Ask every downed channel whether it is back yet.

        This is the answer to "how do you know when Slack came up?". Nothing
        infers it from traffic; a scheduled, side-effect-free probe asks
        directly, on an exponential backoff so a long outage is cheap.
        """

        conn = self.db.writer_conn
        if conn is None:
            return

        async with self.db.write_lock:
            states = await self.health.all_states(conn)

        for state in states:
            if not state.probe_due():
                continue
            probe = _PROBES.get(state.channel)
            if probe is None:
                continue

            healthy = True
            detail = "probe_ok"
            try:
                await probe()
            except Exception as exc:
                healthy = False
                detail = classify(exc).reason

            async with self.db.write_lock:
                new_state = await self.health.record_probe_result(
                    conn, state.channel, healthy, detail
                )
                if new_state.state == HALF_OPEN:
                    await self._prepare_backlog(conn, state.channel)
                await conn.commit()

            logger.info(
                "channel_probe",
                channel=state.channel,
                healthy=healthy,
                detail=detail,
                state=new_state.state,
            )

    async def _prepare_backlog(self, conn, channel: str) -> None:
        """Collapse the queued backlog and queue the digest that explains it."""

        collapsed = await recovery.coalesce_pending(conn, channel)
        outage_start, _ = await self.health.open_outage(conn, channel)
        digest = await recovery.build_recovery_digest(
            conn, channel, outage_start, datetime.now(timezone.utc)
        )
        if digest is not None:
            await recovery.enqueue_recovery_digest(conn, channel, digest)

        logger.info(
            "channel_backlog_prepared",
            channel=channel,
            collapsed=collapsed,
            digest=digest is not None,
        )

    # ── draining ──────────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        now = _utcnow_iso()
        conn = self.db.writer_conn
        if conn is None:
            return

        async with self.db.write_lock:
            available: set[str] = set()
            half_open: set[str] = set()
            for channel in _KNOWN_CHANNELS:
                state = await self.health.get(conn, channel)
                if state.is_available:
                    available.add(channel)
                if state.state == HALF_OPEN:
                    half_open.add(channel)

            if not available:
                return

            # Collapse duplicate intents on every pass, not only after an
            # outage. Payloads are rendered from live state at send time, so
            # several pending rows for one incident would render identically -
            # they are wasted API calls, and storm grouping deliberately
            # produces them by pointing every member at the anchor's card.
            collapsed = 0
            for channel in sorted(available):
                collapsed += await recovery.coalesce_pending(conn, channel)
            if collapsed:
                await conn.commit()

            rows = await self._claim(conn, available, now)

        # A channel on trial gets a small allowance, not the whole backlog, so
        # a provider that is only partially back is caught before we flood it.
        if half_open:
            allowance = self.health.config.half_open_allowance
            trimmed, counts = [], {channel: 0 for channel in half_open}
            for row in rows:
                channel = row["channel"]
                if channel in counts:
                    if counts[channel] >= allowance:
                        continue
                    counts[channel] += 1
                trimmed.append(row)
            rows = trimmed

        for row in rows:
            await self._dispatch(row)

    async def _claim(self, conn, available: set[str], now: str) -> list[dict]:
        """Take an exclusive, expiring lease on a batch of rows.

        Claiming and reading are one statement so two workers cannot both see
        the same row as available. A row is eligible when its lease is absent
        or expired, which is what lets a worker that died mid-dispatch have its
        work picked up rather than stranded.
        """

        placeholders = ", ".join("?" for _ in available)
        lease_until = _iso(
            datetime.now(timezone.utc)
            + timedelta(seconds=settings.OUTBOX_LEASE_SECONDS)
        )

        await conn.execute(
            f"""
            UPDATE outbox
            SET locked_by = ?, locked_until = ?
            WHERE outbox_id IN (
                SELECT outbox_id FROM outbox
                WHERE status = 'pending'
                  AND next_attempt_at <= ?
                  AND channel IN ({placeholders})
                  AND (locked_until IS NULL OR locked_until <= ?)
                ORDER BY priority ASC, outbox_id ASC
                LIMIT ?
            )
            """,
            (
                self.worker_id,
                lease_until,
                now,
                *sorted(available),
                now,
                settings.OUTBOX_BATCH_SIZE,
            ),
        )
        await conn.commit()

        async with conn.execute(
            """
            SELECT outbox_id, incident_id, channel, action, payload_json,
                   attempts, external_ref, priority, origin_channel
            FROM outbox
            WHERE status = 'pending' AND locked_by = ? AND locked_until = ?
            ORDER BY priority ASC, outbox_id ASC
            """,
            (self.worker_id, lease_until),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def _resolve_external_ref(self, conn, incident_id: str, channel: str) -> str | None:
        """Find the provider handle this incident already has on this channel.

        Without this an ``update`` row carries no Slack ``ts`` of its own and
        silently falls back to posting a brand-new message, so one incident
        becomes N messages - the exact fatigue the grouping engine removes
        upstream. The handle belongs to the incident, not to a queue row.
        """

        async with conn.execute(
            """
            SELECT external_ref FROM outbox
            WHERE incident_id = ? AND channel = ? AND status = 'sent'
              AND external_ref IS NOT NULL
            ORDER BY outbox_id DESC LIMIT 1
            """,
            (incident_id, channel),
        ) as cursor:
            row = await cursor.fetchone()
        return row["external_ref"] if row else None

    async def _dispatch(self, row: dict) -> None:
        outbox_id = row["outbox_id"]
        channel = row["channel"]
        action = row["action"]
        payload = json.loads(row["payload_json"])
        incident_id = row["incident_id"]
        conn = self.db.writer_conn

        is_digest = payload.get("kind") == "recovery_digest"

        if not is_digest:
            async with self.db.write_lock:
                payload = await recovery.hydrate_payload(conn, incident_id, payload)
                action = recovery.effective_action(action, payload.get("state"))
                external_ref = row["external_ref"] or await self._resolve_external_ref(
                    conn, incident_id, channel
                )
        else:
            external_ref = row["external_ref"]

        dispatcher = _DISPATCHERS.get(channel)
        if dispatcher is None:
            await self._dead_letter(outbox_id, f"unknown channel: {channel}")
            return

        try:
            new_ref = await dispatcher(action, payload, external_ref)
        except Exception as exc:
            await self._handle_failure(row, exc)
            return

        await self._mark_sent(outbox_id, external_ref=new_ref)
        async with self.db.write_lock:
            _, recovered = await self.health.record_success(conn, channel)
            await conn.commit()

        if recovered:
            logger.info("channel_recovered", channel=channel, incident_id=incident_id)

        logger.info(
            "outbox_delivered",
            outbox_id=outbox_id,
            incident_id=incident_id,
            channel=channel,
            action=action,
        )

    # ── failure handling ──────────────────────────────────────────────────

    async def _handle_failure(self, row: dict, exc: Exception) -> None:
        verdict = classify(exc)
        channel = row["channel"]
        outbox_id = row["outbox_id"]
        conn = self.db.writer_conn

        async with self.db.write_lock:
            state, newly_down = await self.health.record_failure(conn, channel, verdict)
            await conn.commit()

        if verdict.kind is FailureKind.MESSAGE_FATAL:
            # Retrying an identical rejected payload can only fail identically,
            # so this row is dead-lettered now instead of occupying the head of
            # the queue for five rounds of backoff.
            await self._dead_letter(outbox_id, verdict.reason)
            logger.warning(
                "outbox_message_rejected",
                outbox_id=outbox_id,
                channel=channel,
                reason=verdict.reason,
            )
            return

        if verdict.kind is FailureKind.CHANNEL_DOWN:
            # The row did nothing wrong. Park it - attempts untouched - until
            # the breaker's probe says the channel is back. This is what stops
            # a >60s outage from permanently killing the whole backlog.
            await self._park(outbox_id, state.next_probe_at, verdict.reason)
            if newly_down:
                logger.error(
                    "channel_outage_detected",
                    channel=channel,
                    reason=verdict.reason,
                    next_probe_at=state.next_probe_at,
                )
                await self._failover_pending(channel)
            return

        # Transient: this request was unlucky. Charge an attempt and back off,
        # honouring the provider's Retry-After when it gave us one.
        await self._retry_later(
            outbox_id, row["attempts"], verdict.reason, verdict.retry_after_seconds
        )

    async def _failover_pending(self, channel: str) -> None:
        """Re-route urgent queued traffic onto an independent provider.

        Only critical/high rows move. Everything else waits for the primary,
        because failing a `medium` over to PagerDuty would page a human for
        something that was never worth paging for.
        """

        conn = self.db.writer_conn
        async with self.db.write_lock:
            available = {
                candidate
                for candidate in _KNOWN_CHANNELS
                if candidate != channel
                and (await self.health.get(conn, candidate)).is_available
            }
            if not available:
                logger.error("failover_unavailable", channel=channel)
                return

            async with conn.execute(
                """
                SELECT outbox_id, incident_id, action, payload_json, priority
                FROM outbox
                WHERE channel = ? AND status = 'pending' AND failover_of IS NULL
                ORDER BY priority ASC, outbox_id ASC
                """,
                (channel,),
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]

            moved = 0
            now = _utcnow_iso()
            for row in rows:
                # `or` would read critical (priority 0) as the default, which
                # would exclude exactly the rows failover exists for.
                raw_priority = row["priority"]
                priority = 2 if raw_priority is None else int(raw_priority)
                if not is_failover_worthy(priority):
                    continue

                target = select_failover(channel, row["action"], available)
                if target is None:
                    continue

                payload = json.loads(row["payload_json"])
                payload = await recovery.hydrate_payload(
                    conn, row["incident_id"], payload
                )
                payload["failover_from"] = channel

                await conn.execute(
                    """
                    INSERT INTO outbox (
                        incident_id, channel, action, payload_json, status,
                        next_attempt_at, priority, failover_of, origin_channel
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        row["incident_id"],
                        target.channel,
                        target.action,
                        json.dumps(payload, sort_keys=True, separators=(",", ":")),
                        now,
                        priority,
                        row["outbox_id"],
                        channel,
                    ),
                )
                # The original row stays pending. When the primary returns, the
                # incident still gets its card there - marked as already paged
                # elsewhere, so nobody treats it as a second incident.
                await conn.execute(
                    """
                    UPDATE outbox
                    SET payload_json = json_set(payload_json, '$.delivered_via_fallback', ?)
                    WHERE outbox_id = ?
                    """,
                    (target.channel, row["outbox_id"]),
                )
                moved += 1

            if moved:
                await self.health.mark_failed_over(conn, channel, moved)
            await conn.commit()

        logger.warning("failover_completed", channel=channel, rerouted=moved)

    # ── row state transitions ─────────────────────────────────────────────

    async def _mark_sent(self, outbox_id: int, external_ref: str | None) -> None:
        conn = self.db.writer_conn
        now = _utcnow_iso()
        async with self.db.write_lock:
            await conn.execute(
                """
                UPDATE outbox
                SET status = 'sent', sent_at = ?,
                    external_ref = COALESCE(?, external_ref),
                    locked_by = NULL, locked_until = NULL
                WHERE outbox_id = ?
                """,
                (now, external_ref, outbox_id),
            )
            await conn.commit()

    async def _park(self, outbox_id: int, next_probe_at, reason: str) -> None:
        """Hold a row until the channel is expected back. Attempts unchanged."""

        conn = self.db.writer_conn
        # Before the breaker opens there is no probe schedule yet; retry soon so
        # consecutive failures can actually reach the threshold and declare the
        # outage, instead of idling at one failure forever.
        retry_at = next_probe_at or (datetime.now(timezone.utc) + timedelta(seconds=1))
        async with self.db.write_lock:
            await conn.execute(
                """
                UPDATE outbox
                SET next_attempt_at = ?, last_error = ?,
                    locked_by = NULL, locked_until = NULL
                WHERE outbox_id = ?
                """,
                (_iso(retry_at), f"channel_down:{reason}"[:1000], outbox_id),
            )
            await conn.commit()

    async def _retry_later(
        self,
        outbox_id: int,
        attempts: int,
        reason: str,
        retry_after_seconds: float | None,
    ) -> None:
        conn = self.db.writer_conn
        new_attempts = attempts + 1
        status = "dead" if new_attempts >= settings.OUTBOX_MAX_ATTEMPTS else "pending"
        delay = (
            timedelta(seconds=retry_after_seconds)
            if retry_after_seconds
            else _exponential_backoff(new_attempts)
        )
        next_attempt_at = _iso(datetime.now(timezone.utc) + delay)

        async with self.db.write_lock:
            await conn.execute(
                """
                UPDATE outbox
                SET attempts = ?, status = ?, next_attempt_at = ?, last_error = ?,
                    locked_by = NULL, locked_until = NULL
                WHERE outbox_id = ?
                """,
                (new_attempts, status, next_attempt_at, reason[:1000], outbox_id),
            )
            await conn.commit()

    async def _dead_letter(self, outbox_id: int, reason: str) -> None:
        conn = self.db.writer_conn
        async with self.db.write_lock:
            await conn.execute(
                """
                UPDATE outbox SET status = 'dead', last_error = ?,
                    locked_by = NULL, locked_until = NULL
                WHERE outbox_id = ?
                """,
                (reason[:1000], outbox_id),
            )
            await conn.commit()
