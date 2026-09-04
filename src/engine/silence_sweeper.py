"""Infer that an incident is over when nobody ever tells us.

There are exactly three ways a fix can become known to this system:

    1. The monitor says so         - a `resolved` webhook arrives.
    2. A human says so             - Slack or PagerDuty callback (src/inbound).
    3. Nobody says anything        - this module.

The third is the common one, and it was the hole. Plenty of monitoring setups
never send a resolve at all: an alert rule stops matching and simply goes
quiet. Recording rules get deleted mid-incident. A `resolved` webhook is itself
delivered over the network and can be lost. In every one of those cases the
incident stayed open forever, and a permanently-open incident is worse than
noise - it teaches responders that the dashboard is wrong.

So absence of signal is treated as evidence, with two safeguards.

*The threshold is derived, not fixed.* The engine already models each
incident's own arrival rhythm as an EWMA over inter-arrival gaps. An alert that
fires every 5 seconds is overdue after a minute; one that fires hourly is not.
The silence threshold is a multiple of that incident's own predicted gap, so a
chatty service and a quiet one are judged on their own terms rather than
against one global timeout.

*The claim is labelled.* An inferred resolution is written with
``resolution_source='inferred_silence'``, never as though a human confirmed it,
and criticals are held to a much longer threshold - closing a payment outage
because it went quiet for a few minutes is precisely the mistake that would
destroy trust in the system.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import math

import aiosqlite

from src.config import settings
from src.db.connection import Database
from src.inbound.reconcile import RESOLVE, ExternalAction, apply_external_action

logger = logging.getLogger(__name__)

_ACTIVE_STATES = ("OPEN", "ACKNOWLEDGED", "QUIESCENT")
INFERRED_SILENCE = "inferred_silence"


def _parse_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def silence_threshold_ms(
    mean_gap_ms: float,
    variance: float,
    severity: str,
    *,
    multiplier: float,
    critical_multiplier: float,
    floor_ms: int,
    ceiling_ms: int,
) -> int:
    """How long this specific incident may be silent before it is presumed over.

    Built from the incident's own predicted gap plus its observed uncertainty,
    so a noisy signal earns a wider window than a metronomic one. The floor
    stops a brand-new incident with no history from being resolved seconds
    after it opens; the ceiling stops a slow-cycling alert from staying open
    for days. Criticals are stretched by ``critical_multiplier / multiplier``
    on top of everything else.
    """

    if multiplier <= 0:
        raise ValueError("multiplier must be greater than zero")

    predicted = max(0.0, mean_gap_ms) + math.sqrt(max(0.0, variance))

    # The severity stretch is applied *after* the floor, not instead of it.
    # Applying the multiplier first and clamping afterwards would silently
    # collapse both severities onto the same floor for any fast-cycling alert -
    # which is most of them - and a critical would be presumed resolved on
    # exactly the same evidence as a low.
    base = max(predicted * multiplier, float(floor_ms))
    severity_factor = (
        (critical_multiplier / multiplier) if severity == "critical" else 1.0
    )
    return int(min(base * severity_factor, float(ceiling_ms)))


class SilenceSweeper:
    """Periodically closes incidents whose alerts have stopped arriving."""

    def __init__(
        self,
        database: Database,
        *,
        interval_seconds: float | None = None,
    ) -> None:
        self._database = database
        self._interval_seconds = (
            settings.SILENCE_SWEEP_INTERVAL_SECONDS
            if interval_seconds is None
            else interval_seconds
        )
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running or not settings.SILENCE_RESOLVE_ENABLED:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="pulsegraph-silence-sweeper")

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
                logger.exception("silence_sweep_error")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue

    async def _candidates(
        self, tx: aiosqlite.Connection, now_ms: int
    ) -> list[tuple[str, int, int]]:
        placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
        async with tx.execute(
            f"""
            SELECT incident_id, severity, last_alert_at, ewma_mean_gap, ewma_variance
            FROM incidents
            WHERE status IN ({placeholders})
            """,
            _ACTIVE_STATES,
        ) as cursor:
            rows = await cursor.fetchall()

        overdue: list[tuple[str, int, int]] = []
        for row in rows:
            threshold = silence_threshold_ms(
                float(row["ewma_mean_gap"]),
                float(row["ewma_variance"]),
                row["severity"],
                multiplier=settings.SILENCE_RESOLVE_MULTIPLIER,
                critical_multiplier=settings.SILENCE_RESOLVE_CRITICAL_MULTIPLIER,
                floor_ms=settings.SILENCE_RESOLVE_MIN_MS,
                ceiling_ms=settings.SILENCE_RESOLVE_MAX_MS,
            )
            silent_for = now_ms - _parse_ms(row["last_alert_at"])
            if silent_for >= threshold:
                overdue.append((row["incident_id"], silent_for, threshold))
        return overdue

    async def sweep_once(self, *, now_ms: int | None = None) -> int:
        """Close every incident that has gone quiet for long enough.

        Returns how many were resolved. Each one is applied through the same
        reconciliation path a human action takes, so it lands in the audit
        ledger and updates the Slack card like any other transition.
        """

        moment = (
            int(datetime.now(timezone.utc).timestamp() * 1000)
            if now_ms is None
            else now_ms
        )

        async with self._database.write_lock:
            tx = self._database.writer_conn
            if tx is None:
                raise RuntimeError("database writer connection is not available")

            candidates = await self._candidates(tx, moment)
            if not candidates:
                return 0

            resolved = 0
            await tx.execute("BEGIN IMMEDIATE")
            try:
                for incident_id, silent_for, threshold in candidates:
                    seconds = silent_for // 1000
                    action = ExternalAction(
                        # Keyed by the sweep window, so a restart mid-sweep
                        # cannot resolve the same incident twice.
                        inbound_id=f"silence:{incident_id}:{moment // 60000}",
                        provider="system",
                        kind=RESOLVE,
                        incident_id=incident_id,
                        actor=None,
                        detail=(
                            f"No alerts for {seconds}s, past this incident's "
                            f"{threshold // 1000}s silence threshold. "
                            "Presumed resolved - not confirmed by a human."
                        ),
                        resolution_source=INFERRED_SILENCE,
                    )
                    result = await apply_external_action(tx, action)
                    if result.changed:
                        resolved += 1
                await tx.commit()
            except Exception:
                await tx.rollback()
                raise

        if resolved:
            logger.info("silence_sweep_resolved count=%s", resolved)
        return resolved


__all__ = ["INFERRED_SILENCE", "SilenceSweeper", "silence_threshold_ms"]
