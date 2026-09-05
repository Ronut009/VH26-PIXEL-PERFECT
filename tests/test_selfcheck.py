"""Nothing watched the watcher.

PulseGraph could crash, or keep answering HTTP while its drain loop was dead,
and nobody would find out - its silence is indistinguishable from a quiet night.
That is the same ambiguity the silence sweeper resolves for incidents, left
unresolved for the system itself, and it is worse than an ordinary outage
because failures correlate: the event most likely to take PulseGraph down is
the same infrastructure event that should be generating the alerts it is then
failing to deliver.
"""

import os
import tempfile

import httpx
import pytest
import pytest_asyncio

from src.db.connection import Database
from src.selfcheck.health import (
    SelfCheckSignals,
    SelfCheckThresholds,
    Verdict,
    evaluate,
)
from src.selfcheck.heartbeat import HeartbeatEmitter
from src.selfcheck.signals import gather, worker_liveness

ALL_ALIVE = {
    "outbox": True,
    "timer": True,
    "silence_sweeper": True,
    "root_cause": True,
}


@pytest_asyncio.fixture
async def db():
    database = Database(os.path.join(tempfile.mkdtemp(), "selfcheck.db"))
    await database.connect()
    yield database
    await database.close()


# ── being alive is not being healthy ──────────────────────────────────────


def test_a_healthy_system_is_ok():
    report = evaluate(SelfCheckSignals(workers=ALL_ALIVE))

    assert report.verdict is Verdict.OK
    assert report.reasons == ()
    assert report.should_heartbeat


def test_a_dead_drain_loop_is_unhealthy_even_though_http_still_answers():
    """The failure this gap is really about.

    The process is up, ingest keeps accepting alerts, every liveness probe
    passes - and nothing is ever delivered. Only the worker's own state says so.
    """

    report = evaluate(
        SelfCheckSignals(workers={**ALL_ALIVE, "outbox": False})
    )

    assert report.verdict is Verdict.UNHEALTHY
    assert not report.should_heartbeat
    assert any("outbox" in reason for reason in report.reasons)


def test_an_unreachable_database_is_unhealthy():
    report = evaluate(SelfCheckSignals(database_reachable=False, workers=ALL_ALIVE))

    assert report.verdict is Verdict.UNHEALTHY
    assert not report.should_heartbeat


def test_a_stalled_outbox_is_unhealthy():
    report = evaluate(
        SelfCheckSignals(
            workers=ALL_ALIVE, outbox_pending=40, oldest_pending_age_seconds=4000
        ),
        SelfCheckThresholds(stuck_outbox_seconds=900),
    )

    assert report.verdict is Verdict.UNHEALTHY
    assert any("stalled" in reason for reason in report.reasons)


# ── degraded still delivers, so it must not page ──────────────────────────


def test_an_open_breaker_is_degraded_not_unhealthy():
    """Failing over is the delivery plane working, not failing.

    Paging here would punish the system for correctly surviving a provider
    outage - and would fire during precisely the incident a responder is
    already dealing with.
    """

    report = evaluate(
        SelfCheckSignals(workers=ALL_ALIVE, open_channels=("slack",), ongoing_outages=1)
    )

    assert report.verdict is Verdict.DEGRADED
    assert report.should_heartbeat, "degraded still delivers; the switch stays armed"


def test_dead_letters_past_the_limit_are_degraded():
    report = evaluate(
        SelfCheckSignals(workers=ALL_ALIVE, outbox_dead=25),
        SelfCheckThresholds(dead_letter_limit=10),
    )

    assert report.verdict is Verdict.DEGRADED
    assert report.should_heartbeat


def test_a_drifted_source_clock_is_degraded():
    report = evaluate(
        SelfCheckSignals(workers=ALL_ALIVE, worst_clock_skew_ms=-1_200_000),
        SelfCheckThresholds(clock_skew_ms=120_000),
    )

    assert report.verdict is Verdict.DEGRADED
    assert any("clock" in reason for reason in report.reasons)


# ── the honest non-answer ─────────────────────────────────────────────────


def test_silence_from_every_source_is_reported_but_never_paged_on():
    """A quiet night and a severed intake path look identical from in here.

    Guessing wrong is costly either way: page on every quiet night and the
    heartbeat becomes noise; treat a severed intake as calm and the blind spot
    is total. So it is surfaced as an observation and left to a human.
    """

    report = evaluate(
        SelfCheckSignals(workers=ALL_ALIVE, seconds_since_last_ingest=7200),
        SelfCheckThresholds(quiet_ingest_seconds=3600),
    )

    assert report.verdict is Verdict.OK, "silence alone is not a failure"
    assert report.reasons == ()
    assert any("cannot be told apart" in note for note in report.observations)


# ── the dead man's switch ─────────────────────────────────────────────────


class _Recorder:
    """Stands in for the external watchdog."""

    def __init__(self, fail: bool = False) -> None:
        self.pings = 0
        self._fail = fail

    async def get(self, url):
        self.pings += 1
        if self._fail:
            raise httpx.ConnectError("watchdog unreachable")
        return httpx.Response(200, request=httpx.Request("GET", url))

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_a_healthy_system_reports_in(db):
    watchdog = _Recorder()
    emitter = HeartbeatEmitter(
        db, None, url="https://watchdog.example/ping", client=watchdog
    )

    assert await emitter.beat_once() is True
    assert watchdog.pings == 1
    assert emitter.last_verdict is Verdict.OK


@pytest.mark.asyncio
async def test_the_switch_goes_silent_when_alerts_are_not_getting_out(db):
    """The inversion the whole design rests on.

    A heartbeat driven by liveness would keep insisting all is well while the
    drain loop is dead - an active all-clear, which is worse than no heartbeat
    at all. Silence is what pages.
    """

    class DeadWorkers:
        worker = None
        timer_worker = None
        silence_sweeper = None
        root_cause_worker = None

    watchdog = _Recorder()
    emitter = HeartbeatEmitter(
        db, DeadWorkers(), url="https://watchdog.example/ping", client=watchdog
    )

    assert await emitter.beat_once() is False
    assert watchdog.pings == 0, "a system that cannot deliver must not report in"
    assert emitter.last_verdict is Verdict.UNHEALTHY
    assert emitter.suppressed_count == 1


@pytest.mark.asyncio
async def test_an_unreachable_watchdog_is_not_our_failure(db):
    """We are fine; the watchdog is not answering.

    It will page on the missing ping, which is the right outcome anyway, so
    this must not be recorded as ill health.
    """

    watchdog = _Recorder(fail=True)
    emitter = HeartbeatEmitter(
        db, None, url="https://watchdog.example/ping", client=watchdog
    )

    assert await emitter.beat_once() is False
    assert emitter.last_verdict is Verdict.OK
    assert emitter.last_sent_ok is False
    assert emitter.suppressed_count == 0, "not a suppression - a delivery failure"


@pytest.mark.asyncio
async def test_an_unconfigured_switch_says_so_rather_than_pretending(db):
    emitter = HeartbeatEmitter(db, None, url="")

    assert emitter.enabled is False
    emitter.start()
    assert emitter.running is False, "nothing to run without a watchdog to ping"


# ── gathering must not fail when the system is unwell ─────────────────────


@pytest.mark.asyncio
async def test_signals_are_readable_from_a_live_database(db):
    signals = await gather(db)

    assert signals.database_reachable
    assert signals.outbox_pending == 0
    assert signals.open_channels == ()


@pytest.mark.asyncio
async def test_a_closed_database_reports_rather_than_raises(db):
    """A self-check that throws when the system is unwell reports nothing
    exactly when it matters most."""

    await db.close()

    signals = await gather(db)

    assert signals.database_reachable is False


def test_worker_liveness_counts_a_missing_worker_as_dead():
    class PartialState:
        worker = None

        class _Alive:
            running = True

        timer_worker = _Alive()
        silence_sweeper = _Alive()
        root_cause_worker = _Alive()

    liveness = worker_liveness(PartialState())

    assert liveness["outbox"] is False
    assert liveness["timer"] is True
