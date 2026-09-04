from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio

from src.contracts import NormalizedEvent
from src.db.writer import DbWriter
from src.graph.edge_decay import DecayedWeights, decay_weights, increment_weights
from src.graph.root_cause_ranker import rank_root_cause

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


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
        "pod_uid": f"{alertname}-uid",
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


def test_decay_math_halves_evidence_at_one_half_life() -> None:
    decayed = decay_weights(8.0, 4.0, 2.0, elapsed_ms=1_000, half_life_ms=1_000)
    incremented = increment_weights(
        DecayedWeights(8.0, 4.0, 2.0), elapsed_ms=1_000, half_life_ms=1_000
    )

    assert decayed == DecayedWeights(joint=4.0, source=2.0, target=1.0)
    assert incremented == DecayedWeights(joint=5.0, source=3.0, target=2.0)


@pytest.mark.asyncio
async def test_storm_replay_creates_directed_edge_and_ranks_leader(db_conn) -> None:
    writer = DbWriter()
    started_at = datetime(2026, 9, 4, tzinfo=timezone.utc)

    source_result = await writer.process_event(db_conn, _event("GatewayLatency", started_at))
    target_result = await writer.process_event(
        db_conn, _event("DatabasePoolExhausted", started_at + timedelta(seconds=1))
    )

    async with db_conn.execute("SELECT * FROM edges") as cursor:
        edges = await cursor.fetchall()
    hint = await rank_root_cause(db_conn)

    assert len(edges) == 1
    assert edges[0]["src_incident_id"] == source_result["incident_id"]
    assert edges[0]["dst_incident_id"] == target_result["incident_id"]
    assert edges[0]["weight"] == pytest.approx(1.0)
    assert hint is not None
    assert source_result["incident_id"] in hint


@pytest.mark.asyncio
async def test_critical_bypass_does_not_create_graph_edges(db_conn) -> None:
    writer = DbWriter()
    event = _event("ServiceDown", datetime(2026, 9, 4, tzinfo=timezone.utc)).model_copy(
        update={"severity_raw": "critical"}
    )

    await writer.process_event(db_conn, event)

    async with db_conn.execute("SELECT COUNT(*) AS count FROM edges") as cursor:
        count = (await cursor.fetchone())["count"]
    assert count == 0
