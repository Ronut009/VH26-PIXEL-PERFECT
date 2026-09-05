"""The dead man's switch: report in while healthy, go silent when not.

Every other alert in this system is a message we send. This one is the
*absence* of a message, and that inversion is the entire point - a failure that
stops PulseGraph also stops the heartbeat, so the failure announces itself. A
system that has to be working in order to report that it is broken cannot
report the failures that matter most.

Three properties this depends on, in order of how easily they are lost:

**The check must live outside.** The heartbeat is sent to an external service
(PagerDuty heartbeats, healthchecks.io, a Cloudwatch alarm) that pages when the
ping stops arriving. Nothing here can page anyone about itself - by definition
the interesting case is the one where "here" is gone.

**It must be gated on doing the job, not on being alive.** A liveness ping
proves the process is running, which it will be while the outbox stalls and
every breaker sits open. That is worse than no heartbeat, because it is an
active all-clear. This one stops when ``SelfCheckReport`` says alerts are not
getting out.

**It must not travel through the machinery it is checking.** The heartbeat is a
direct HTTP call, never an outbox row. Routing it through the delivery plane
would mean a stalled outbox also stalls the heartbeat - which happens to page,
but for the wrong reason and only by luck, and a heartbeat queued behind a
backlog would report a system that has been dead for an hour as fine.

Failures correlate: the event most likely to take PulseGraph down is the same
infrastructure event that should be generating the alerts it is failing to
deliver. That is also the argument for running it outside the blast radius of
what it monitors, which is a deployment decision rather than a code one.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from src.config import settings
from src.db.connection import Database

from .health import Verdict, evaluate
from .signals import gather

logger = logging.getLogger(__name__)


class HeartbeatEmitter:
    """Pings an external watchdog for as long as delivery is working."""

    def __init__(
        self,
        database: Database,
        app_state=None,
        *,
        url: str | None = None,
        interval_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._database = database
        self._app_state = app_state
        self._url = url if url is not None else settings.HEARTBEAT_URL
        self._interval_seconds = (
            settings.HEARTBEAT_INTERVAL_SECONDS
            if interval_seconds is None
            else interval_seconds
        )
        self._owns_client = client is None
        self._client = client
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Exposed for the self-check endpoint, so an operator can see whether
        # the switch is actually armed rather than assuming it.
        self.last_verdict: Verdict | None = None
        self.last_sent_ok: bool | None = None
        self.suppressed_count = 0

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        if not self.enabled:
            # Not an error, but worth saying out loud: without this, nothing
            # anywhere notices if PulseGraph stops.
            logger.warning(
                "heartbeat_disabled url_unset=1 detail=%s",
                "HEARTBEAT_URL is unset, so no external watchdog will notice "
                "if this process dies",
            )
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="pulsegraph-heartbeat")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.beat_once()
            except Exception:
                # Never let a heartbeat failure take down the emitter: the next
                # tick is the retry, and a missed beat is already the signal.
                logger.exception("heartbeat_error")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue

    async def beat_once(self) -> bool:
        """Evaluate health and ping if entitled to. Returns whether it pinged."""

        signals = await gather(self._database, self._app_state)
        report = evaluate(signals)
        self.last_verdict = report.verdict

        if not report.should_heartbeat:
            self.suppressed_count += 1
            self.last_sent_ok = None
            # Loud, because from here on the only signal is silence, and
            # somebody reading the logs afterwards needs to know it was
            # deliberate rather than a crash.
            logger.error(
                "heartbeat_withheld verdict=%s reasons=%s",
                report.verdict.value,
                "; ".join(report.reasons),
            )
            return False

        if not self.enabled:
            return False

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.HEARTBEAT_TIMEOUT_SECONDS)

        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
        except Exception as exc:
            # We are healthy but could not reach the watchdog. Do not treat
            # that as our own failure; the watchdog will page on the missing
            # ping, which is the correct outcome either way.
            self.last_sent_ok = False
            logger.warning("heartbeat_send_failed error=%s", str(exc))
            return False

        self.last_sent_ok = True
        return True


__all__ = ["HeartbeatEmitter"]
