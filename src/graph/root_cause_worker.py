"""Recompute root-cause hints outside the write transaction.

Ranking used to run inside the ``BEGIN IMMEDIATE`` that persists an alert,
holding the single SQLite writer lock that every ingest request queues behind.
Bounding the neighbourhood stopped that cost growing with the size of a storm,
but it left the more basic problem in place: a root cause is an **enrichment**,
not a transactional invariant. Nothing about durably recording an alert, or
about delivering its notification, depends on knowing what caused it.

Moving it here buys two things.

*The write transaction gets shorter.* Every alert paid for a ranking pass while
holding the lock. Now it pays for the edge and marginal updates only.

*Ranking is debounced.* Five hundred alerts arriving in a storm used to trigger
five hundred rankings of a neighbourhood that barely changed between them. A
scope is marked dirty by the observation round and swept at most once per
interval, so the same storm triggers a handful. The work per alert stops
scaling with the alert rate entirely.

Nothing is lost by the delay. Delivery payloads are rendered from live incident
state at send time, so a hint that lands after a card was queued still appears
on it - the same property that lets a card recovered after an outage show the
current alert count rather than a stale one.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

import aiosqlite

from src.config import settings
from src.db.connection import Database

from .root_cause_ranker import RootCauseVerdict, score_root_cause

logger = logging.getLogger(__name__)

_ACTIVE_STATES = ("OPEN", "ACKNOWLEDGED", "QUIESCENT")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _iso_ms(epoch_ms: int) -> str:
    moment = datetime.fromtimestamp(max(0, epoch_ms) / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


async def describe_verdict(
    tx: aiosqlite.Connection, verdict: RootCauseVerdict
) -> str:
    """Render a verdict for a human, not for a log parser.

    The hint is shown verbatim on the Slack card, and a bare UUID tells a
    responder nothing at 3am. The incident's title is what they recognise; the
    confidence is what lets them decide how much to trust it.
    """

    async with tx.execute(
        "SELECT title FROM incidents WHERE incident_id = ?", (verdict.incident_id,)
    ) as cursor:
        row = await cursor.fetchone()

    label = row["title"] if row and row["title"] else verdict.incident_id
    return f"{label} (confidence {verdict.confidence:.0%})"


async def _dirty_scopes(tx: aiosqlite.Connection) -> list[tuple[str, int]]:
    """Scopes observed since they were last ranked, with the revision seen.

    Dirtiness is a revision comparison rather than a timestamp one because
    ``last_observed_at`` carries the monitor's clock while any "last ranked"
    stamp is wall clock. A source running a few minutes behind would leave its
    scope permanently clean and root cause would quietly stop updating - a
    counter has no clock to disagree with.

    The revision is returned so the sweep records exactly what it consumed. Any
    round landing afterwards leaves the scope dirty for the next pass instead
    of being marked done by a sweep that never saw it.
    """

    async with tx.execute(
        """
        SELECT scope_key, observed_revision FROM graph_scope_stats
        WHERE ranked_revision < observed_revision
        """
    ) as cursor:
        return [
            (row["scope_key"], int(row["observed_revision"]))
            for row in await cursor.fetchall()
        ]


async def rank_scope(
    tx: aiosqlite.Connection,
    scope_key: str,
    now_ms: int,
    *,
    ranked_revision: int | None = None,
) -> int:
    """Recompute and store the hint for one scope's recent neighbourhood.

    Returns how many incidents were updated. The neighbourhood is the same
    bounded window the observation round uses, so ranking sees exactly the
    evidence that was gathered rather than a wider or staler slice.
    """

    window_start = _iso_ms(now_ms - settings.CORRELATION_WINDOW_MS)
    placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
    async with tx.execute(
        f"""
        SELECT incident_id
        FROM incidents
        WHERE scope_key = ? AND status IN ({placeholders})
          AND COALESCE(last_ingested_at, last_alert_at) >= ?
        ORDER BY COALESCE(last_ingested_at, last_alert_at) DESC
        LIMIT ?
        """,
        (
            scope_key,
            *_ACTIVE_STATES,
            window_start,
            settings.CORRELATION_MAX_NEIGHBOURS,
        ),
    ) as cursor:
        candidates = [row["incident_id"] for row in await cursor.fetchall()]

    verdict = await score_root_cause(tx, candidate_ids=candidates)
    # A cause is a property of the cascade, not of each member, so every
    # incident in the neighbourhood carries the same answer - including
    # ``None``, which is how a hint that no longer holds gets withdrawn rather
    # than lingering as a stale claim.
    hint = await describe_verdict(tx, verdict) if verdict is not None else None

    updated = 0
    if candidates:
        candidate_placeholders = ", ".join("?" for _ in candidates)
        cursor = await tx.execute(
            f"""
            UPDATE incidents SET root_cause_hint = ?
            WHERE incident_id IN ({candidate_placeholders})
              AND root_cause_hint IS NOT ?
            """,
            (hint, *candidates, hint),
        )
        updated = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    if ranked_revision is None:
        async with tx.execute(
            "SELECT observed_revision FROM graph_scope_stats WHERE scope_key = ?",
            (scope_key,),
        ) as cursor:
            row = await cursor.fetchone()
        ranked_revision = int(row["observed_revision"]) if row else 0

    await tx.execute(
        """
        UPDATE graph_scope_stats
        SET ranked_revision = ?, ranked_at = ?
        WHERE scope_key = ?
        """,
        (ranked_revision, _iso(datetime.now(timezone.utc)), scope_key),
    )
    return updated


class RootCauseWorker:
    """Debounced background ranking for every scope with new evidence."""

    def __init__(
        self, database: Database, *, interval_seconds: float | None = None
    ) -> None:
        self._database = database
        self._interval_seconds = (
            settings.ROOT_CAUSE_SWEEP_INTERVAL_SECONDS
            if interval_seconds is None
            else interval_seconds
        )
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="pulsegraph-root-cause-worker"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.sweep_once()
            except Exception:
                logger.exception("root_cause_sweep_error")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue

    async def sweep_once(self, *, now_ms: int | None = None) -> int:
        """Rank every dirty scope once. Returns incidents updated."""

        moment = (
            int(datetime.now(timezone.utc).timestamp() * 1000)
            if now_ms is None
            else now_ms
        )

        async with self._database.write_lock:
            tx = self._database.writer_conn
            if tx is None:
                raise RuntimeError("database writer connection is not available")

            scopes = await _dirty_scopes(tx)
            if not scopes:
                return 0

            updated = 0
            await tx.execute("BEGIN IMMEDIATE")
            try:
                for scope_key, revision in scopes:
                    updated += await rank_scope(
                        tx, scope_key, moment, ranked_revision=revision
                    )
                await tx.commit()
            except Exception:
                await tx.rollback()
                raise

        if updated:
            logger.info(
                "root_cause_sweep scopes=%s incidents_updated=%s", len(scopes), updated
            )
        return updated


__all__ = ["RootCauseWorker", "describe_verdict", "rank_scope"]
