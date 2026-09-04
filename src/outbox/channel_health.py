"""Per-channel circuit breaker: detects an outage, then detects recovery.

Three states, one row per channel in ``channel_health``:

    CLOSED     normal. Rows dispatch freely. Channel-level failures accumulate.
    OPEN       the channel is considered down. The worker stops dispatching to
               it entirely - no row burns an attempt against a dead endpoint -
               and a probe is scheduled on an exponential backoff.
    HALF_OPEN  a probe just succeeded. A small number of real rows are allowed
               through as a trial. One channel-level failure sends us back to
               OPEN; a success closes the breaker and ends the outage.

The important property is that recovery is *detected*, not guessed. A probe is
a cheap, side-effect-free call (Slack ``auth.test``) so polling a dead channel
every few seconds never posts anything to a real channel and never pages anyone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite

from src.outbox.failure_policy import FailureVerdict

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class BreakerConfig:
    # Consecutive channel-level failures before we declare an outage. More than
    # one, so a single unlucky request never stops a healthy channel.
    failure_threshold: int = 3
    # First probe delay after opening, then doubling up to probe_max_seconds.
    probe_base_seconds: float = 5.0
    probe_max_seconds: float = 120.0
    # Rows allowed through during the HALF_OPEN trial before we fully close.
    half_open_allowance: int = 3


@dataclass(frozen=True)
class ChannelState:
    channel: str
    state: str
    consecutive_failures: int
    probe_backoff_step: int
    opened_at: datetime | None
    next_probe_at: datetime | None
    last_error: str | None

    @property
    def is_available(self) -> bool:
        """Whether the worker may dispatch ordinary rows to this channel."""

        return self.state in (CLOSED, HALF_OPEN)

    def probe_due(self, now: datetime | None = None) -> bool:
        if self.state != OPEN:
            return False
        if self.next_probe_at is None:
            return True
        return (now or _now()) >= self.next_probe_at


def _probe_delay(config: BreakerConfig, step: int) -> timedelta:
    seconds = min(config.probe_base_seconds * (2**step), config.probe_max_seconds)
    return timedelta(seconds=seconds)


class ChannelHealthStore:
    """Reads and writes breaker state. The caller holds the write lock."""

    def __init__(self, config: BreakerConfig | None = None) -> None:
        self._config = config or BreakerConfig()

    @property
    def config(self) -> BreakerConfig:
        return self._config

    async def get(self, conn: aiosqlite.Connection, channel: str) -> ChannelState:
        async with conn.execute(
            """
            SELECT channel, state, consecutive_failures, probe_backoff_step,
                   opened_at, next_probe_at, last_error
            FROM channel_health WHERE channel = ?
            """,
            (channel,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return ChannelState(channel, CLOSED, 0, 0, None, None, None)

        return _row_to_state(row)

    async def all_states(self, conn: aiosqlite.Connection) -> list[ChannelState]:
        async with conn.execute(
            """
            SELECT channel, state, consecutive_failures, probe_backoff_step,
                   opened_at, next_probe_at, last_error
            FROM channel_health
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_state(row) for row in rows]

    async def _upsert(self, conn: aiosqlite.Connection, channel: str, **fields) -> None:
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        assignments = ", ".join(f"{name} = excluded.{name}" for name in fields)
        await conn.execute(
            f"""
            INSERT INTO channel_health (channel, {columns}, updated_at)
            VALUES (?, {placeholders}, ?)
            ON CONFLICT(channel) DO UPDATE SET {assignments}, updated_at = excluded.updated_at
            """,
            (channel, *fields.values(), _iso(_now())),
        )

    async def record_success(
        self, conn: aiosqlite.Connection, channel: str
    ) -> tuple[ChannelState, bool]:
        """Register a successful delivery. Returns (new state, channel recovered)."""

        before = await self.get(conn, channel)
        now = _now()
        recovered = before.state in (OPEN, HALF_OPEN)

        if recovered:
            await self._close_outage(conn, channel, now)

        await self._upsert(
            conn,
            channel,
            state=CLOSED,
            consecutive_failures=0,
            probe_backoff_step=0,
            opened_at=None,
            next_probe_at=None,
            last_success_at=_iso(now),
        )
        return await self.get(conn, channel), recovered

    async def record_failure(
        self, conn: aiosqlite.Connection, channel: str, verdict: FailureVerdict
    ) -> tuple[ChannelState, bool]:
        """Register a failure. Returns (new state, outage newly detected)."""

        before = await self.get(conn, channel)
        now = _now()

        if not verdict.trips_breaker:
            # Transient or message-fatal: the channel is not implicated, so the
            # failure counter is left alone. A poison payload must never be
            # able to convince us that Slack is down.
            await self._upsert(
                conn,
                channel,
                state=before.state,
                consecutive_failures=before.consecutive_failures,
                probe_backoff_step=before.probe_backoff_step,
                opened_at=_iso(before.opened_at) if before.opened_at else None,
                next_probe_at=(
                    _iso(before.next_probe_at) if before.next_probe_at else None
                ),
                last_failure_at=_iso(now),
                last_error=verdict.reason,
            )
            return await self.get(conn, channel), False

        failures = before.consecutive_failures + 1

        # A failure during the HALF_OPEN trial means recovery was premature.
        # Go straight back to OPEN and lengthen the probe interval.
        if before.state == HALF_OPEN:
            step = before.probe_backoff_step + 1
            await self._upsert(
                conn,
                channel,
                state=OPEN,
                consecutive_failures=failures,
                probe_backoff_step=step,
                opened_at=_iso(before.opened_at or now),
                next_probe_at=_iso(now + _probe_delay(self._config, step)),
                last_failure_at=_iso(now),
                last_error=verdict.reason,
            )
            return await self.get(conn, channel), False

        if before.state == OPEN or failures < self._config.failure_threshold:
            step = before.probe_backoff_step
            next_probe = before.next_probe_at
            if before.state == OPEN:
                step += 1
                next_probe = now + _probe_delay(self._config, step)
            await self._upsert(
                conn,
                channel,
                state=before.state,
                consecutive_failures=failures,
                probe_backoff_step=step,
                opened_at=_iso(before.opened_at) if before.opened_at else None,
                next_probe_at=_iso(next_probe) if next_probe else None,
                last_failure_at=_iso(now),
                last_error=verdict.reason,
            )
            return await self.get(conn, channel), False

        # Threshold crossed from CLOSED: this is the moment we declare it down.
        delay = verdict.retry_after_seconds
        next_probe = now + (
            timedelta(seconds=delay) if delay else _probe_delay(self._config, 0)
        )
        queue_depth = await self._queue_depth(conn, channel)
        await self._upsert(
            conn,
            channel,
            state=OPEN,
            consecutive_failures=failures,
            probe_backoff_step=0,
            opened_at=_iso(now),
            next_probe_at=_iso(next_probe),
            last_failure_at=_iso(now),
            last_error=verdict.reason,
            outage_count=await self._next_outage_count(conn, channel),
        )
        await conn.execute(
            """
            INSERT INTO channel_outages (channel, detected_at, last_error, queued_at_peak)
            VALUES (?, ?, ?, ?)
            """,
            (channel, _iso(now), verdict.reason, queue_depth),
        )
        return await self.get(conn, channel), True

    async def record_probe_result(
        self, conn: aiosqlite.Connection, channel: str, healthy: bool, detail: str
    ) -> ChannelState:
        """Apply the outcome of a side-effect-free reachability probe."""

        before = await self.get(conn, channel)
        now = _now()

        if healthy:
            # Do not close outright. Let real traffic prove it in HALF_OPEN, so
            # a provider that answers auth.test but rejects posts is caught.
            await self._upsert(
                conn,
                channel,
                state=HALF_OPEN,
                consecutive_failures=0,
                probe_backoff_step=before.probe_backoff_step,
                opened_at=_iso(before.opened_at) if before.opened_at else None,
                next_probe_at=None,
                last_success_at=_iso(now),
            )
        else:
            step = before.probe_backoff_step + 1
            await self._upsert(
                conn,
                channel,
                state=OPEN,
                consecutive_failures=before.consecutive_failures,
                probe_backoff_step=step,
                opened_at=_iso(before.opened_at or now),
                next_probe_at=_iso(now + _probe_delay(self._config, step)),
                last_failure_at=_iso(now),
                last_error=detail,
            )
            await conn.execute(
                """
                UPDATE channel_outages SET probe_attempts = probe_attempts + 1
                WHERE outage_id = (
                    SELECT outage_id FROM channel_outages
                    WHERE channel = ? AND recovered_at IS NULL
                    ORDER BY outage_id DESC LIMIT 1
                )
                """,
                (channel,),
            )
        return await self.get(conn, channel)

    async def open_outage(
        self, conn: aiosqlite.Connection, channel: str
    ) -> tuple[datetime | None, int]:
        """Return (outage start, rows queued) for the outage still in progress."""

        async with conn.execute(
            """
            SELECT detected_at, queued_at_peak FROM channel_outages
            WHERE channel = ? AND recovered_at IS NULL
            ORDER BY outage_id DESC LIMIT 1
            """,
            (channel,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None, 0
        return _parse(row["detected_at"]), int(row["queued_at_peak"])

    async def mark_failed_over(
        self, conn: aiosqlite.Connection, channel: str, count: int
    ) -> None:
        await conn.execute(
            """
            UPDATE channel_outages SET failed_over = failed_over + ?
            WHERE outage_id = (
                SELECT outage_id FROM channel_outages
                WHERE channel = ? AND recovered_at IS NULL
                ORDER BY outage_id DESC LIMIT 1
            )
            """,
            (count, channel),
        )

    async def _close_outage(
        self, conn: aiosqlite.Connection, channel: str, now: datetime
    ) -> None:
        await conn.execute(
            """
            UPDATE channel_outages SET recovered_at = ?
            WHERE outage_id = (
                SELECT outage_id FROM channel_outages
                WHERE channel = ? AND recovered_at IS NULL
                ORDER BY outage_id DESC LIMIT 1
            )
            """,
            (_iso(now), channel),
        )

    async def _next_outage_count(self, conn: aiosqlite.Connection, channel: str) -> int:
        async with conn.execute(
            "SELECT outage_count FROM channel_health WHERE channel = ?", (channel,)
        ) as cursor:
            row = await cursor.fetchone()
        return (int(row["outage_count"]) if row else 0) + 1

    async def _queue_depth(self, conn: aiosqlite.Connection, channel: str) -> int:
        async with conn.execute(
            "SELECT COUNT(*) AS depth FROM outbox WHERE channel = ? AND status = 'pending'",
            (channel,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["depth"]) if row else 0


def _row_to_state(row) -> ChannelState:
    return ChannelState(
        channel=row["channel"],
        state=row["state"],
        consecutive_failures=int(row["consecutive_failures"]),
        probe_backoff_step=int(row["probe_backoff_step"]),
        opened_at=_parse(row["opened_at"]),
        next_probe_at=_parse(row["next_probe_at"]),
        last_error=row["last_error"],
    )


__all__ = [
    "CLOSED",
    "HALF_OPEN",
    "OPEN",
    "BreakerConfig",
    "ChannelHealthStore",
    "ChannelState",
]
