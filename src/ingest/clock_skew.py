"""Measure how far each source's clock sits from ours, and say so.

Every alert carries two timestamps: ``fired_at``, from the monitor, and the
moment we received it. Their difference is clock drift, and until this existed
that drift was completely invisible - it defeated adaptive batching and
auto-resolved live incidents with nothing in the logs to explain why. The
symptom looked like a product bug; the cause was a VM whose clock had slipped.

Separating event time from processing time stops drift from *breaking* things.
Recording it here stops drift from *hiding*. A monitoring system that trusts
remote clocks without measuring them has a blind spot exactly where it is
supposed to have vision - and the team running it would have no way to know
which of their exporters is the one drifting.

Skew is negative when the source is behind us and positive when it runs ahead.
"""

from __future__ import annotations

from datetime import datetime, timezone
import time

import aiosqlite

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Last time we warned about each source, so a persistently drifted clock
# reports itself once in a while instead of once per alert. Process-local by
# design: this is log hygiene, not state worth persisting.
_last_warned_at: dict[tuple[str, str], float] = {}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _should_warn(source: str, scope_key: str, skew_ms: int) -> bool:
    if abs(skew_ms) < settings.CLOCK_SKEW_WARN_MS:
        return False

    key = (source, scope_key)
    now = time.monotonic()
    previous = _last_warned_at.get(key)
    if previous is not None and now - previous < settings.CLOCK_SKEW_WARN_INTERVAL_SECONDS:
        return False

    _last_warned_at[key] = now
    return True


async def record_skew(
    tx: aiosqlite.Connection,
    *,
    source: str,
    scope_key: str,
    fired_at: datetime,
    ingested_at: datetime | None = None,
) -> int:
    """Record one observation of a source's clock offset. Returns the skew.

    Written as a single upsert with no preceding read: this runs on the alert
    write path, and a read-modify-write per alert would be a real cost for a
    diagnostic.
    """

    received = ingested_at or datetime.now(timezone.utc)
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)

    skew_ms = int((fired_at - received).total_seconds() * 1000)

    await tx.execute(
        """
        INSERT INTO source_clock_skew (
            source, scope_key, last_skew_ms, max_abs_skew_ms, samples, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(source, scope_key) DO UPDATE SET
            last_skew_ms = excluded.last_skew_ms,
            max_abs_skew_ms = MAX(
                source_clock_skew.max_abs_skew_ms, excluded.max_abs_skew_ms
            ),
            samples = source_clock_skew.samples + 1,
            updated_at = excluded.updated_at
        """,
        (source, scope_key, skew_ms, abs(skew_ms), _iso(received)),
    )

    if _should_warn(source, scope_key, skew_ms):
        logger.warning(
            "source_clock_skew",
            source=source,
            scope_key=scope_key,
            skew_ms=skew_ms,
            detail=(
                "source clock is "
                f"{'ahead of' if skew_ms > 0 else 'behind'} ours by "
                f"{abs(skew_ms) // 1000}s; elapsed-time judgements about this "
                "source's alerts are computed from arrival time, but its "
                "reported timestamps will look wrong on the dashboard"
            ),
        )

    return skew_ms


async def worst_offenders(
    tx: aiosqlite.Connection, *, limit: int = 10
) -> list[dict]:
    """Sources ordered by how far their clocks have ever been from ours."""

    async with tx.execute(
        """
        SELECT source, scope_key, last_skew_ms, max_abs_skew_ms, samples, updated_at
        FROM source_clock_skew
        ORDER BY max_abs_skew_ms DESC
        LIMIT ?
        """,
        (limit,),
    ) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


__all__ = ["record_skew", "worst_offenders"]
