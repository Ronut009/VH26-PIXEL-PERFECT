"""Two correct features composing into a noise generator.

The silence sweeper closes an incident whose alerts stopped arriving, and
`_next_state` reopens a RESOLVED incident when the next alert lands. Each is
right on its own. Together, a service flapping on a cycle longer than its
silence threshold resolves and reopens indefinitely, and every transition posts
a card update - so the system built to stop alert fatigue becomes a source of
it.

A flapping alert is also a finding in its own right. Repeated close/reopen
almost always means the alert threshold is wrong, which is an alert-quality
problem this system is uniquely placed to notice and nobody else is.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from src.config import settings
from src.contracts import NormalizedEvent
from src.db.connection import Database
from src.db.writer import DbWriter

START = datetime(2026, 9, 4, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def db():
    database = Database(os.path.join(tempfile.mkdtemp(), "flap.db"))
    await database.connect()
    yield database
    await database.close()


def _event(status: str, fired_at: datetime) -> NormalizedEvent:
    labels = {"environment": "production", "cluster": "c1"}
    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint="fp-flapper",
        source="prometheus",
        service="orders-api",
        alertname="LatencyHigh",
        severity_raw="warning",
        status=status,
        labels=labels,
        message="threshold crossed",
        fired_at=fired_at,
        raw_payload={"labels": labels},
    )


async def _ingest(db: Database, status: str, at: datetime) -> dict:
    async with db.write_lock:
        return await DbWriter().process_event(db.writer_conn, _event(status, at))


async def _flap(db: Database, cycles: int) -> None:
    """Resolve and reopen the same incident, the way a bad threshold does."""

    for index in range(cycles):
        base = START + timedelta(minutes=index * 30)
        await _ingest(db, "resolved", base)
        await _ingest(db, "firing", base + timedelta(minutes=15))


async def _slack_rows(db: Database) -> int:
    async with db.writer_conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE channel = 'slack'"
    ) as cursor:
        return int((await cursor.fetchone())["n"])


async def _incident(db: Database) -> dict:
    async with db.writer_conn.execute(
        "SELECT status, reopen_count, flapping_since FROM incidents"
    ) as cursor:
        return dict(await cursor.fetchone())


# ── the churn ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_flapping_alert_stops_posting_a_card_per_transition(db):
    """The bug: every close and reopen is its own notification.

    Ten cycles of a badly-thresholded alert produced twenty-one card updates
    for a single incident that nobody needs told about twenty-one times.
    """

    await _ingest(db, "firing", START)
    await _flap(db, cycles=10)
    after_ten = await _slack_rows(db)

    # 21 transitions produced 21 card updates before damping existed. A handful
    # is expected now: the early ones are genuinely new information and still
    # notify, and only the repeats past the threshold are collapsed.
    assert after_ten < 10, f"{after_ten} card updates for 21 transitions"

    # The property that actually matters is that it stays collapsed. A service
    # can flap for hours; the cost must not keep growing with it.
    await _flap(db, cycles=10)
    after_twenty = await _slack_rows(db)

    assert after_twenty == after_ten, (
        f"twenty more transitions added {after_twenty - after_ten} card updates; "
        "damping has to hold for as long as the flapping does"
    )


@pytest.mark.asyncio
async def test_reopening_is_counted(db):
    await _ingest(db, "firing", START)
    await _flap(db, cycles=3)

    incident = await _incident(db)

    assert incident["reopen_count"] == 3
    assert incident["flapping_since"] is not None, (
        "a repeatedly reopened incident should be marked as flapping"
    )


@pytest.mark.asyncio
async def test_a_stable_incident_is_never_marked_flapping(db):
    """Damping must not fire on an ordinary incident that resolves once."""

    await _ingest(db, "firing", START)
    await _ingest(db, "resolved", START + timedelta(minutes=10))
    await _ingest(db, "firing", START + timedelta(minutes=20))

    incident = await _incident(db)

    assert incident["reopen_count"] == 1
    assert incident["flapping_since"] is None, "one reopen is not a pattern"


@pytest.mark.asyncio
async def test_the_first_transitions_still_notify(db):
    """Damping suppresses repeats, not the news itself.

    The first close and the first reopen are genuinely new information and must
    still reach the channel.
    """

    await _ingest(db, "firing", START)
    first = await _slack_rows(db)

    await _ingest(db, "resolved", START + timedelta(minutes=10))
    await _ingest(db, "firing", START + timedelta(minutes=20))

    assert await _slack_rows(db) > first, "early transitions are still reported"


# ── hysteresis: closing gets harder ───────────────────────────────────────


def test_closing_gets_harder_each_time_an_incident_reopens():
    """Otherwise the sweeper keeps closing the same incident on the same
    evidence, and the reopen is guaranteed to follow."""

    from src.engine.silence_sweeper import silence_threshold_ms

    kwargs = dict(
        multiplier=6.0,
        critical_multiplier=20.0,
        floor_ms=900_000,
        ceiling_ms=21_600_000,
        hysteresis_factor=1.5,
        hysteresis_max_reopens=6,
    )

    stable = silence_threshold_ms(5_000.0, 0.0, "high", reopen_count=0, **kwargs)
    once = silence_threshold_ms(5_000.0, 0.0, "high", reopen_count=1, **kwargs)
    many = silence_threshold_ms(5_000.0, 0.0, "high", reopen_count=5, **kwargs)

    assert once > stable
    assert many > once
    assert many <= kwargs["ceiling_ms"], "hysteresis still respects the ceiling"


# ── the finding ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_card_says_the_alert_itself_is_the_problem(db):
    """A flapping alert usually means the threshold is wrong.

    That is an alert-quality finding, and this system is the only thing in the
    stack positioned to notice it.
    """

    from src.outbox.recovery import hydrate_payload
    from src.outbox.slack import _build_blocks

    await _ingest(db, "firing", START)
    await _flap(db, cycles=5)

    async with db.writer_conn.execute("SELECT incident_id FROM incidents") as cursor:
        incident_id = (await cursor.fetchone())["incident_id"]

    payload = await hydrate_payload(
        db.writer_conn, incident_id, {"incident_id": incident_id}
    )
    assert payload.get("flapping") is True

    import json

    rendered = json.dumps(_build_blocks(payload))
    assert "lapping" in rendered, "the card should name the pattern"
    assert "threshold" in rendered, "and point at the likely cause"


@pytest.mark.asyncio
async def test_damping_is_configurable_off(db, monkeypatch):
    """An operator who wants every transition should be able to have them."""

    monkeypatch.setattr(settings, "FLAP_DAMPING_ENABLED", False)

    await _ingest(db, "firing", START)
    await _flap(db, cycles=6)

    assert await _slack_rows(db) > 6, "with damping off, every transition notifies"
