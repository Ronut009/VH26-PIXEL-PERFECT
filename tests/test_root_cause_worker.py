"""Ranking belongs off the write path, and should cost per storm, not per alert.

Root cause used to be computed inside the transaction that persists an alert,
holding the single SQLite writer lock every ingest request queues behind. It is
an enrichment, not a transactional invariant: nothing about durably recording
an alert or delivering its notification depends on knowing what caused it.

The bigger win is debouncing. Five hundred alerts in a storm used to trigger
five hundred rankings of a neighbourhood that barely changed between them.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio

from src.contracts import NormalizedEvent
from src.db.connection import Database
from src.db.writer import DbWriter
from src.graph import root_cause_worker
from src.graph.root_cause_worker import RootCauseWorker, rank_scope

SCOPE = "production/c1"
START = datetime(2026, 9, 4, tzinfo=timezone.utc)
SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


def _event(alertname: str, fired_at: datetime) -> NormalizedEvent:
    labels = {"environment": "production", "cluster": "c1", "pod": f"{alertname}-p"}
    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint=f"i-{alertname}",
        source="prometheus",
        service="orders-api",
        alertname=alertname,
        severity_raw="warning",
        status="firing",
        labels=labels,
        message="threshold exceeded",
        fired_at=fired_at,
        raw_payload={"labels": labels},
    )


@pytest_asyncio.fixture
async def db_conn() -> aiosqlite.Connection:
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield connection
    await connection.close()


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "rootcause.db"))
    await database.connect()
    yield database
    await database.close()


async def _drive_cascade(conn: aiosqlite.Connection, rounds: int = 6) -> None:
    """A repeated DbPool -> Latency cascade, the shape a real one has."""

    writer = DbWriter()
    for index in range(rounds):
        base = START + timedelta(seconds=index * 10)
        await writer.process_event(conn, _event("DbPoolExhausted", base))
        await writer.process_event(conn, _event("LatencyHigh", base + timedelta(seconds=1)))


async def _hints(conn: aiosqlite.Connection) -> list[str | None]:
    async with conn.execute("SELECT root_cause_hint FROM incidents") as cursor:
        return [row["root_cause_hint"] for row in await cursor.fetchall()]


# ── the write path no longer ranks ────────────────────────────────────────


@pytest.mark.asyncio
async def test_persisting_an_alert_does_not_rank(db_conn):
    """The transaction that records an alert should not also explain it."""

    await _drive_cascade(db_conn)

    assert all(hint is None for hint in await _hints(db_conn)), (
        "ranking must not happen inside the write transaction"
    )


@pytest.mark.asyncio
async def test_the_background_pass_fills_the_hint_in(db_conn):
    await _drive_cascade(db_conn)

    updated = await rank_scope(
        db_conn, SCOPE, int((START + timedelta(seconds=60)).timestamp() * 1000)
    )

    hints = [hint for hint in await _hints(db_conn) if hint]
    assert updated > 0
    assert hints, "the sweep should store what the write path no longer does"
    assert "DbPoolExhausted" in hints[0]


# ── debouncing is the point ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_storm_earns_one_ranking_pass_not_one_per_alert(db, monkeypatch):
    """The property that makes this worth moving at all."""

    calls = {"count": 0}
    original = root_cause_worker.score_root_cause

    async def counted(*args, **kwargs):
        calls["count"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(root_cause_worker, "score_root_cause", counted)

    conn = db.writer_conn
    await _drive_cascade(conn, rounds=20)
    alerts = 40

    assert calls["count"] == 0, "no ranking happened while the alerts were arriving"

    await RootCauseWorker(db).sweep_once(
        now_ms=int((START + timedelta(seconds=300)).timestamp() * 1000)
    )

    assert calls["count"] == 1, (
        f"{alerts} alerts should collapse to one ranking pass, not {calls['count']}"
    )


@pytest.mark.asyncio
async def test_a_scope_with_no_new_evidence_is_not_re_ranked(db, monkeypatch):
    """Once ranked, a quiet scope costs nothing until something changes."""

    calls = {"count": 0}
    original = root_cause_worker.score_root_cause

    async def counted(*args, **kwargs):
        calls["count"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(root_cause_worker, "score_root_cause", counted)

    conn = db.writer_conn
    await _drive_cascade(conn)
    worker = RootCauseWorker(db)
    now_ms = int((START + timedelta(seconds=300)).timestamp() * 1000)

    await worker.sweep_once(now_ms=now_ms)
    assert calls["count"] == 1

    await worker.sweep_once(now_ms=now_ms)
    await worker.sweep_once(now_ms=now_ms)
    assert calls["count"] == 1, "a scope with no new evidence must not be re-ranked"


@pytest.mark.asyncio
async def test_new_evidence_makes_a_scope_dirty_again(db):
    conn = db.writer_conn
    await _drive_cascade(conn)
    worker = RootCauseWorker(db)
    now_ms = int((START + timedelta(seconds=300)).timestamp() * 1000)

    await worker.sweep_once(now_ms=now_ms)

    async with conn.execute(
        "SELECT observed_revision, ranked_revision FROM graph_scope_stats"
        " WHERE scope_key = ?",
        (SCOPE,),
    ) as cursor:
        before = dict(await cursor.fetchone())
    assert before["ranked_revision"] == before["observed_revision"], "swept clean"

    writer = DbWriter()
    await writer.process_event(
        conn, _event("DbPoolExhausted", START + timedelta(seconds=400))
    )

    async with conn.execute(
        "SELECT observed_revision, ranked_revision FROM graph_scope_stats"
        " WHERE scope_key = ?",
        (SCOPE,),
    ) as cursor:
        after = dict(await cursor.fetchone())

    assert after["observed_revision"] > after["ranked_revision"], (
        "a new observation should mark the scope dirty for the next sweep"
    )


@pytest.mark.asyncio
async def test_dirtiness_survives_a_source_whose_clock_runs_behind(db):
    """The reason dirtiness is a counter and not a timestamp.

    `last_observed_at` carries the monitor's clock; any "last ranked" stamp is
    wall clock. Compared against each other, a source running behind leaves its
    scope permanently clean and root cause silently stops updating - which is
    exactly what happened when this was first written with timestamps.
    """

    conn = db.writer_conn
    worker = RootCauseWorker(db)

    # Every alert here is stamped in the past relative to the wall clock.
    await _drive_cascade(conn)
    assert await worker.sweep_once(now_ms=int(START.timestamp() * 1000) + 300_000) >= 0

    writer = DbWriter()
    await writer.process_event(
        conn, _event("LatencyHigh", START + timedelta(seconds=500))
    )

    assert await _dirty(conn), (
        "a scope with new evidence must be dirty regardless of whose clock is ahead"
    )


async def _dirty(conn: aiosqlite.Connection) -> bool:
    async with conn.execute(
        "SELECT 1 FROM graph_scope_stats WHERE ranked_revision < observed_revision"
    ) as cursor:
        return await cursor.fetchone() is not None


# ── a hint that stops holding is withdrawn ────────────────────────────────


@pytest.mark.asyncio
async def test_a_hint_that_no_longer_holds_is_cleared(db_conn):
    """A stale conclusion left on a card is worse than no conclusion."""

    await _drive_cascade(db_conn)
    now_ms = int((START + timedelta(seconds=60)).timestamp() * 1000)
    await rank_scope(db_conn, SCOPE, now_ms)
    assert any(await _hints(db_conn))

    # The evidence goes away - the cascade resolved and the edges with it.
    await db_conn.execute("DELETE FROM edges")
    await db_conn.commit()

    await rank_scope(db_conn, SCOPE, now_ms)

    assert all(hint is None for hint in await _hints(db_conn)), (
        "withdrawing a hint matters as much as publishing one"
    )
