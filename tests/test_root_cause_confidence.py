"""Root cause must distinguish a cause from something that is merely always on.

The ranker summed raw outbound co-occurrence weight. That cannot tell "this led
to that" from "this fires constantly, so it co-occurs with everything", and it
cannot tell a strong conclusion from a coin flip. During a broad event - a bad
deploy, an AZ blip - everything co-occurs with everything, so the loudest node
wins and the card states it as fact. A responder told the wrong root cause once
stops reading the field, which is worse than not having the field at all.
"""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio

from src.graph.root_cause_ranker import rank_root_cause

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@pytest_asyncio.fixture
async def db_conn() -> aiosqlite.Connection:
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield connection
    await connection.close()


async def _edge(conn, src: str, dst: str, weight: float) -> None:
    await conn.execute(
        "INSERT INTO edges (src_incident_id, dst_incident_id, weight, last_seen_at)"
        " VALUES (?, ?, ?, ?)",
        (src, dst, weight, _iso(datetime.now(timezone.utc))),
    )


@pytest.mark.asyncio
async def test_a_node_that_is_always_on_is_not_a_root_cause(db_conn):
    """The bug this work exists to fix.

    `noisy` co-occurs with everything: it leads sometimes and follows just as
    often, which is what "this service is always unhealthy" looks like in the
    graph. `cause` leads a smaller cascade and never follows anything.

    Ranked by raw outbound weight, `noisy` wins with 20 against `cause`'s 12,
    and the card confidently names a service that caused nothing. Weighing how
    much a node *leads* against how much it *follows* is what separates them.
    """

    noisy, cause = str(uuid4()), str(uuid4())
    a, b, x, y = (str(uuid4()) for _ in range(4))

    # `noisy` both leads and follows - it is simply always firing.
    await _edge(db_conn, noisy, a, 10.0)
    await _edge(db_conn, noisy, b, 10.0)
    await _edge(db_conn, a, noisy, 10.0)
    await _edge(db_conn, b, noisy, 10.0)

    # `cause` only ever leads.
    await _edge(db_conn, cause, x, 6.0)
    await _edge(db_conn, cause, y, 6.0)
    await db_conn.commit()

    hint = await rank_root_cause(
        db_conn, candidate_ids=(noisy, cause, a, b, x, y)
    )

    assert hint is not None
    assert cause in hint, "a node that leads should outrank one that merely co-occurs"
    assert noisy not in hint


@pytest.mark.asyncio
async def test_a_single_co_occurrence_is_a_coincidence_not_a_cause(db_conn):
    """One co-occurrence is not evidence, and the honest answer is silence."""

    first, second = str(uuid4()), str(uuid4())
    await _edge(db_conn, first, second, 1.0)
    await db_conn.commit()

    assert await rank_root_cause(db_conn, candidate_ids=(first, second)) is None


@pytest.mark.asyncio
async def test_repeated_co_occurrence_does_produce_a_leader(db_conn):
    """Evidence that accumulates should eventually be enough to speak."""

    leader, follower = str(uuid4()), str(uuid4())
    await _edge(db_conn, leader, follower, 5.0)
    await db_conn.commit()

    hint = await rank_root_cause(db_conn, candidate_ids=(leader, follower))

    assert hint is not None
    assert leader in hint


@pytest.mark.asyncio
async def test_an_indistinguishable_field_reports_no_root_cause(db_conn):
    """A broad event makes everything co-occur; nothing there is a conclusion.

    Four services that all lead and follow each other equally is exactly what a
    bad deploy or an availability-zone blip looks like. Naming any one of them
    would be picking arbitrarily and calling it analysis.
    """

    nodes = [str(uuid4()) for _ in range(4)]
    for src in nodes:
        for dst in nodes:
            if src != dst:
                await _edge(db_conn, src, dst, 8.0)
    await db_conn.commit()

    assert await rank_root_cause(db_conn, candidate_ids=tuple(nodes)) is None


@pytest.mark.asyncio
async def test_the_hint_carries_its_confidence(db_conn):
    """A claim a responder can weigh, not a bare assertion."""

    leader, follower = str(uuid4()), str(uuid4())
    await _edge(db_conn, leader, follower, 9.0)
    await db_conn.commit()

    hint = await rank_root_cause(db_conn, candidate_ids=(leader, follower))

    assert hint is not None
    assert "confidence=" in hint


# ── what a responder actually reads ───────────────────────────────────────


@pytest.mark.asyncio
async def test_the_stored_hint_names_the_incident_not_a_uuid(db_conn):
    """The hint is rendered verbatim on the Slack card.

    A bare UUID and a raw weight tell a responder nothing at 3am. They need the
    name of the thing they can go and look at, and enough of the evidence to
    decide whether to believe it.
    """

    from datetime import timedelta

    from src.contracts import NormalizedEvent
    from src.db.writer import DbWriter

    def _event(alertname: str, fired_at) -> NormalizedEvent:
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

    writer = DbWriter()
    start = datetime(2026, 9, 4, tzinfo=timezone.utc)
    for round_index in range(6):
        base = start + timedelta(seconds=round_index * 10)
        await writer.process_event(db_conn, _event("DbPoolExhausted", base))
        await writer.process_event(
            db_conn, _event("LatencyHigh", base + timedelta(seconds=1))
        )

    async with db_conn.execute(
        "SELECT root_cause_hint FROM incidents WHERE root_cause_hint IS NOT NULL LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()

    assert row is not None, "a repeated two-service cascade should reach a verdict"
    hint = row["root_cause_hint"]
    assert "DbPoolExhausted" in hint, "the leader is named by its title"
    assert "confidence" in hint
    assert "root_cause=" not in hint, "the machine format is not what a human reads"
