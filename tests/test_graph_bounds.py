"""Correlation cost must not grow with the size of the storm.

The graph ran unbounded on every alert: every active incident in the scope
became a correlation neighbour, and root cause was re-ranked over every edge
between active incidents - all inside the write transaction holding the single
SQLite writer lock that ingest queues behind. Both grew with the square of the
active set, so the graph was at its slowest during exactly the storm it exists
to absorb. A demo with four incidents never shows it.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio

from src.config import settings
from src.contracts import NormalizedEvent
from src.db.writer import DbWriter
from src.graph.root_cause_ranker import rank_root_cause

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"
SCOPE = "production/storm-cluster"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@pytest_asyncio.fixture
async def db_conn() -> aiosqlite.Connection:
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield connection
    await connection.close()


def _event(alertname: str, fired_at: datetime) -> NormalizedEvent:
    labels = {
        "environment": "production",
        "cluster": "storm-cluster",
        "pod": f"{alertname}-pod",
    }
    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint=f"ingest-{alertname}",
        source="prometheus",
        service="checkout-api",
        alertname=alertname,
        severity_raw="warning",
        status="firing",
        labels=labels,
        message=f"{alertname} firing",
        fired_at=fired_at,
        raw_payload={"labels": labels},
    )


async def _seed_active(
    conn: aiosqlite.Connection, count: int, *, minutes_ago: float
) -> list[str]:
    """Fill a scope with active incidents, as a real storm would."""

    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ids = []
    for index in range(count):
        incident_id = str(uuid4())
        ids.append(incident_id)
        await conn.execute(
            """
            INSERT INTO incidents (
                incident_id, scope_key, stable_fingerprint, title, summary,
                severity, status, alert_count, first_alert_at, last_alert_at
            ) VALUES (?, ?, ?, ?, 'summary', 'medium', 'ACKNOWLEDGED', 1, ?, ?)
            """,
            (
                incident_id,
                SCOPE,
                f"crowd-fingerprint-{index}-{minutes_ago}",
                f"noise-{index}",
                _iso(moment),
                _iso(moment),
            ),
        )
    await conn.commit()
    return ids


@pytest.mark.asyncio
async def test_one_alert_correlates_against_a_capped_neighbourhood(db_conn):
    """With a large active set, per-alert edge work stays constant."""

    crowd = settings.CORRELATION_MAX_NEIGHBOURS * 3
    await _seed_active(db_conn, crowd, minutes_ago=1)

    writer = DbWriter()
    result = await writer.process_event(
        db_conn, _event("LatencyHigh", datetime.now(timezone.utc))
    )
    incident_id = result["incident_id"]

    async with db_conn.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE src_incident_id = ? OR dst_incident_id = ?",
        (incident_id, incident_id),
    ) as cursor:
        edges = (await cursor.fetchone())["n"]

    assert edges <= settings.CORRELATION_MAX_NEIGHBOURS, (
        f"one alert created {edges} edges against {crowd} active incidents; "
        "per-alert cost must not grow with the size of the storm"
    )
    assert edges > 0, "correlation must still happen, just bounded"


@pytest.mark.asyncio
async def test_incidents_outside_the_window_are_not_correlated(db_conn):
    """Co-occurrence means "at the same time", so old incidents are not evidence."""

    stale_minutes = (settings.CORRELATION_WINDOW_MS / 60_000) + 30
    await _seed_active(db_conn, 5, minutes_ago=stale_minutes)

    writer = DbWriter()
    result = await writer.process_event(
        db_conn, _event("LatencyHigh", datetime.now(timezone.utc))
    )
    incident_id = result["incident_id"]

    async with db_conn.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE src_incident_id = ? OR dst_incident_id = ?",
        (incident_id, incident_id),
    ) as cursor:
        edges = (await cursor.fetchone())["n"]

    assert edges == 0, "an incident from an hour ago did not co-occur with this one"


@pytest.mark.asyncio
async def test_edge_growth_stays_linear_across_a_storm(db_conn):
    """The property that matters: total edges grow with alerts, not alerts squared."""

    await _seed_active(db_conn, settings.CORRELATION_MAX_NEIGHBOURS * 2, minutes_ago=1)

    writer = DbWriter()
    now = datetime.now(timezone.utc)
    for index in range(10):
        await writer.process_event(
            db_conn, _event(f"Cascade{index}", now + timedelta(milliseconds=index))
        )

    async with db_conn.execute("SELECT COUNT(*) AS n FROM edges") as cursor:
        total = (await cursor.fetchone())["n"]

    ceiling = 10 * settings.CORRELATION_MAX_NEIGHBOURS
    assert total <= ceiling, f"{total} edges from 10 alerts exceeds the {ceiling} cap"


# ── ranking is scoped, which is cheaper and also more correct ─────────────


@pytest.mark.asyncio
async def test_ranking_a_neighbourhood_ignores_a_louder_unrelated_incident(db_conn):
    """A global rank names the loudest thing anywhere, not this cascade's cause.

    That is a correctness bug as much as a cost one: the hint on a card would
    describe a different event entirely.
    """

    database, api, pods = (str(uuid4()) for _ in range(3))
    loud_a, loud_b = str(uuid4()), str(uuid4())
    seen = _iso(datetime.now(timezone.utc))

    for src, dst, weight in (
        (database, api, 3.0),
        (database, pods, 2.5),
        (api, pods, 1.0),
        # An unrelated incident elsewhere in the scope with far more evidence.
        (loud_a, loud_b, 500.0),
    ):
        await db_conn.execute(
            "INSERT INTO edges (src_incident_id, dst_incident_id, weight, last_seen_at)"
            " VALUES (?, ?, ?, ?)",
            (src, dst, weight, seen),
        )
    await db_conn.commit()

    scoped = await rank_root_cause(db_conn, candidate_ids=(database, api, pods))
    assert scoped is not None
    assert database in scoped
    assert loud_a not in scoped, "the unrelated loud incident is not this cause"


@pytest.mark.asyncio
async def test_a_neighbourhood_too_small_to_have_a_leader_returns_nothing(db_conn):
    """Silence beats a confident wrong answer."""

    assert await rank_root_cause(db_conn, candidate_ids=()) is None
    assert await rank_root_cause(db_conn, candidate_ids=(str(uuid4()),)) is None


@pytest.mark.asyncio
async def test_the_unrestricted_path_is_still_capped(db_conn):
    """The global path keeps a backstop so it degrades the hint, not the write."""

    seen = _iso(datetime.now(timezone.utc))
    src = str(uuid4())
    for index in range(30):
        dst = str(uuid4())
        for incident_id in (src, dst):
            await db_conn.execute(
                """
                INSERT OR IGNORE INTO incidents (
                    incident_id, scope_key, stable_fingerprint, title, summary,
                    severity, status, alert_count, first_alert_at, last_alert_at
                ) VALUES (?, ?, ?, 'n', 's', 'medium', 'ACKNOWLEDGED', 1, ?, ?)
                """,
                (incident_id, SCOPE, f"fp-{incident_id}", seen, seen),
            )
        await db_conn.execute(
            "INSERT INTO edges (src_incident_id, dst_incident_id, weight, last_seen_at)"
            " VALUES (?, ?, ?, ?)",
            (src, dst, 1.0 + index, seen),
        )
    await db_conn.commit()

    assert await rank_root_cause(db_conn, max_edges=5) is not None
