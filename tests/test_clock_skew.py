"""Scheduling must not depend on a third party's clock.

Every timestamp in this system is one of two different things, and they were
being compared to each other:

* **event time** - `fired_at`, from the monitor. What the source believes.
* **processing time** - `ingested_at`, from us. When we actually saw it.

They only coincide when every monitor's clock is correct and nothing is ever
delayed. A monitoring system that silently trusts remote clocks has a blind
spot exactly where it is supposed to have vision, and the failures are not
graceful: a source running a few minutes behind gets its live incidents closed.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from src.contracts import NormalizedEvent
from src.db.connection import Database
from src.db.writer import DbWriter
from src.engine.silence_sweeper import SilenceSweeper

# Far enough behind to cross the silence floor, which is the point: this is not
# an absurd clock, it is a VM that has drifted.
SKEW = timedelta(minutes=20)


@pytest_asyncio.fixture
async def db():
    directory = tempfile.mkdtemp()
    database = Database(os.path.join(directory, "skew.db"))
    await database.connect()
    yield database
    await database.close()


def _event(alertname: str, fired_at: datetime) -> NormalizedEvent:
    labels = {"environment": "production", "cluster": "c1"}
    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint=f"fp-{alertname}",
        source="prometheus",
        service="orders-api",
        alertname=alertname,
        severity_raw="warning",
        status="firing",
        labels=labels,
        message="firing right now",
        fired_at=fired_at,
        raw_payload={"labels": labels},
    )


async def _ingest(db: Database, event: NormalizedEvent) -> dict:
    async with db.write_lock:
        return await DbWriter().process_event(db.writer_conn, event)


# ── the serious one ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_slow_source_clock_does_not_resolve_a_live_incident(db):
    """The worst failure this gap produces.

    The alert is firing *now*. Its monitor's clock reads twenty minutes ago.
    Measuring silence as `wall_clock_now - fired_at` makes a brand-new incident
    look like it has been quiet for twenty minutes, past the fifteen-minute
    floor, so the sweeper closes it and the card says "presumed resolved".

    The alerting system silences a live production incident because a VM
    drifted. No attacker required.
    """

    await _ingest(db, _event("LatencyHigh", datetime.now(timezone.utc) - SKEW))

    resolved = await SilenceSweeper(db).sweep_once()

    async with db.writer_conn.execute("SELECT status FROM incidents") as cursor:
        status = (await cursor.fetchone())["status"]

    assert resolved == 0, "a just-arrived alert is not a silent one"
    assert status != "RESOLVED", "a live incident must not be closed by clock drift"


@pytest.mark.asyncio
async def test_silence_is_still_detected_when_arrivals_genuinely_stop(db):
    """The fix must not simply disable the feature it protects."""

    await _ingest(db, _event("LatencyHigh", datetime.now(timezone.utc)))

    # Nothing has arrived for hours in *our* clock, which is what silence means.
    future_ms = int(
        (datetime.now(timezone.utc) + timedelta(hours=4)).timestamp() * 1000
    )
    resolved = await SilenceSweeper(db).sweep_once(now_ms=future_ms)

    async with db.writer_conn.execute("SELECT status FROM incidents") as cursor:
        status = (await cursor.fetchone())["status"]

    assert resolved == 1
    assert status == "RESOLVED"


# ── batching ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_skewed_clock_does_not_defeat_batching(db):
    """A deadline is a promise about our future, not the monitor's past.

    `quiet_at_ms` is fired against wall clock by the timer wheel. Computed from
    a source clock running behind, it lands in the past and the incident fires
    immediately - defeating the adaptive batching that is the whole product.
    """

    result = await _ingest(db, _event("LatencyHigh", datetime.now(timezone.utc) - SKEW))

    quiet_at_ms = result["quiet_at_ms"]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    assert quiet_at_ms is not None
    assert quiet_at_ms > now_ms, (
        "a quiet deadline computed from a skewed source clock landed in the past, "
        "so the incident fires immediately and never batches"
    )


@pytest.mark.asyncio
async def test_a_clock_running_ahead_does_not_delay_delivery(db):
    """The mirror failure: a fast clock postpones a real notification."""

    result = await _ingest(db, _event("LatencyHigh", datetime.now(timezone.utc) + SKEW))

    quiet_at_ms = result["quiet_at_ms"]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    assert quiet_at_ms is not None
    # The window itself is seconds; anything near the skew means we inherited it.
    assert quiet_at_ms - now_ms < SKEW.total_seconds() * 1000 / 2, (
        "a source clock running ahead pushed the deadline into its future too"
    )


# ── gap statistics still belong to the source ─────────────────────────────


@pytest.mark.asyncio
async def test_inter_arrival_gaps_are_still_measured_in_event_time(db):
    """Only *scheduling* moves to our clock.

    How fast an alert repeats is a property of the source, and its own clock is
    the right measure of its own cadence - a constant offset cancels out in a
    difference. Recomputing gaps from arrival time would let network jitter
    rewrite the EWMA.
    """

    base = datetime.now(timezone.utc) - SKEW
    await _ingest(db, _event("LatencyHigh", base))
    await _ingest(db, _event("LatencyHigh", base + timedelta(seconds=30)))

    async with db.writer_conn.execute(
        "SELECT gap_history_json, ewma_mean_gap FROM incidents"
    ) as cursor:
        row = dict(await cursor.fetchone())

    import json

    gaps = json.loads(row["gap_history_json"])
    assert gaps, "a second alert should have recorded an inter-arrival gap"
    assert gaps[-1] == pytest.approx(30_000, rel=0.01), (
        "the gap is the source's own 30s, not the interval we happened to see"
    )


# ── drift becomes visible ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drift_is_recorded_so_the_offending_source_can_be_found(db):
    """Separating the clocks stops drift breaking things; this stops it hiding.

    Otherwise the team has a system that quietly behaves oddly and no way to
    learn which of their exporters is the one that slipped.
    """

    from src.ingest.clock_skew import worst_offenders

    await _ingest(db, _event("LatencyHigh", datetime.now(timezone.utc) - SKEW))

    rows = await worst_offenders(db.writer_conn)

    assert rows, "the skew observation should have been recorded"
    row = rows[0]
    assert row["source"] == "prometheus"
    assert row["scope_key"] == "production/c1"
    # Negative: this source's clock is behind ours.
    assert row["last_skew_ms"] < 0
    assert row["max_abs_skew_ms"] == pytest.approx(
        SKEW.total_seconds() * 1000, rel=0.05
    )


@pytest.mark.asyncio
async def test_a_healthy_clock_records_a_negligible_skew(db):
    from src.ingest.clock_skew import worst_offenders

    await _ingest(db, _event("LatencyHigh", datetime.now(timezone.utc)))

    row = (await worst_offenders(db.writer_conn))[0]

    assert abs(row["last_skew_ms"]) < 5_000, "a correct clock is not an incident"


@pytest.mark.asyncio
async def test_the_worst_drift_is_remembered_not_just_the_latest(db):
    """A clock that slips and recovers still needs explaining."""

    from src.ingest.clock_skew import worst_offenders

    await _ingest(db, _event("LatencyHigh", datetime.now(timezone.utc) - SKEW))
    await _ingest(db, _event("LatencyHigh", datetime.now(timezone.utc)))

    row = (await worst_offenders(db.writer_conn))[0]

    assert row["samples"] == 2
    assert abs(row["last_skew_ms"]) < 5_000, "the latest observation is healthy"
    assert row["max_abs_skew_ms"] == pytest.approx(
        SKEW.total_seconds() * 1000, rel=0.05
    ), "but the worst seen is retained"


def test_skew_warnings_are_throttled_per_source():
    """A persistently drifted clock should report itself, not bury the log."""

    from src.config import settings
    from src.ingest import clock_skew

    clock_skew._last_warned_at.clear()
    big = settings.CLOCK_SKEW_WARN_MS + 1_000

    assert clock_skew._should_warn("prometheus", "production/c1", big)
    assert not clock_skew._should_warn("prometheus", "production/c1", big)
    # A different source is a different problem and warns on its own.
    assert clock_skew._should_warn("datadog", "production/c1", big)
    # A healthy clock never warns at all.
    assert not clock_skew._should_warn("grafana", "production/c1", 1_000)
