"""Read PulseGraph's own internal state.

Most of this was already recorded and none of it was reachable. ``outbox``
knows what is undelivered, ``channel_health`` knows which providers are down,
``channel_outages`` knows what is still open, and ``source_clock_skew`` knows
which exporter drifted - but there was no way to ask, so an operator's only
view of the alerting system was whether its HTTP port answered.

Gathering runs read-only and never raises: a self-check that fails when the
system is unwell is a self-check that reports nothing exactly when it matters.
A signal that cannot be read becomes its own finding instead.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.db.connection import Database
from src.outbox.channel_health import OPEN, ChannelHealthStore
from src.utils.logging import get_logger

from .health import SelfCheckSignals

logger = get_logger(__name__)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_seconds(value: str | None, now: datetime) -> float | None:
    moment = _parse(value)
    if moment is None:
        return None
    return max(0.0, (now - moment).total_seconds())


def worker_liveness(app_state) -> dict[str, bool]:
    """Which background tasks are still running.

    A dead background task is the failure this whole gap is about: HTTP keeps
    answering, ingest keeps accepting alerts, and nothing is ever delivered.
    Nothing about that is visible from outside the process.
    """

    def _alive(worker) -> bool:
        if worker is None:
            return False
        running = getattr(worker, "running", None)
        if running is not None:
            return bool(running)
        # OutboxWorker keeps only the raw task handle.
        task = getattr(worker, "_task", None)
        return task is not None and not task.done()

    return {
        "outbox": _alive(getattr(app_state, "worker", None)),
        "timer": _alive(getattr(app_state, "timer_worker", None)),
        "silence_sweeper": _alive(getattr(app_state, "silence_sweeper", None)),
        "root_cause": _alive(getattr(app_state, "root_cause_worker", None)),
    }


async def gather(database: Database, app_state=None) -> SelfCheckSignals:
    """Collect every internal signal. Never raises."""

    now = datetime.now(timezone.utc)
    workers = worker_liveness(app_state) if app_state is not None else {}

    conn = database.writer_conn
    if conn is None:
        return SelfCheckSignals(database_reachable=False, workers=workers)

    try:
        async with database.write_lock:
            await conn.execute("SELECT 1")

            async with conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END) AS dead,
                    MIN(CASE WHEN status = 'pending' THEN created_at END) AS oldest
                FROM outbox
                """
            ) as cursor:
                outbox = dict(await cursor.fetchone())

            async with conn.execute(
                "SELECT COUNT(*) AS n FROM channel_outages WHERE recovered_at IS NULL"
            ) as cursor:
                outages = int((await cursor.fetchone())["n"])

            # Ingest recency comes from processing time: when we last *received*
            # something, never when a monitor claims it fired.
            async with conn.execute(
                "SELECT MAX(ingested_at) AS latest FROM raw_events"
            ) as cursor:
                last_ingest = (await cursor.fetchone())["latest"]

            async with conn.execute(
                "SELECT MAX(ABS(last_skew_ms)) AS worst FROM source_clock_skew"
            ) as cursor:
                worst_skew = (await cursor.fetchone())["worst"] or 0

            async with conn.execute(
                "SELECT COUNT(*) AS n FROM graph_scope_stats"
                " WHERE ranked_revision < observed_revision"
            ) as cursor:
                unranked = int((await cursor.fetchone())["n"])

            store = ChannelHealthStore()
            open_channels = tuple(
                sorted(
                    state.channel
                    for state in await store.all_states(conn)
                    if state.state == OPEN
                )
            )
    except Exception as exc:
        # The database being unreadable *is* the answer, not a reason to fail.
        logger.warning("selfcheck_gather_failed", error=str(exc))
        return SelfCheckSignals(database_reachable=False, workers=workers)

    return SelfCheckSignals(
        database_reachable=True,
        workers=workers,
        outbox_pending=int(outbox["pending"] or 0),
        outbox_dead=int(outbox["dead"] or 0),
        oldest_pending_age_seconds=_age_seconds(outbox["oldest"], now),
        open_channels=open_channels,
        ongoing_outages=outages,
        seconds_since_last_ingest=_age_seconds(last_ingest, now),
        worst_clock_skew_ms=int(worst_skew),
        unranked_scopes=unranked,
    )


__all__ = ["gather", "worker_liveness"]
