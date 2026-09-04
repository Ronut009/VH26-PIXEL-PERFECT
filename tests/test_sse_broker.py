from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio

from src.contracts import NormalizedEvent
from src.db.writer import DbWriter
from src.stream.sse_broker import read_delta_events, read_snapshot

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
        "cluster": "stream-cluster",
        "pod": f"{alertname}-pod",
        "pod_uid": f"{alertname}-uid",
    }
    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint=f"ingest-{alertname}",
        source="prometheus",
        service="catalog-api",
        alertname=alertname,
        severity_raw="warning",
        status="firing",
        labels=labels,
        message=f"{alertname} firing",
        fired_at=fired_at,
        raw_payload={"labels": labels},
    )


@pytest.mark.asyncio
async def test_snapshot_returns_bounded_dashboard_state(db_conn) -> None:
    writer = DbWriter()
    await writer.process_event(
        db_conn, _event("CatalogLatency", datetime(2026, 9, 4, tzinfo=timezone.utc))
    )

    snapshot = await read_snapshot(db_conn)

    assert snapshot.event_type == "snapshot"
    assert snapshot.stream_id == 10
    assert len(snapshot.data["incidents"]) == 1
    assert snapshot.data["incidents"][0]["state"] == "ACKNOWLEDGED"
    assert "streamId" in snapshot.encode()


@pytest.mark.asyncio
async def test_deltas_emit_monotonic_incident_graph_card_and_metrics_events(db_conn) -> None:
    writer = DbWriter()
    started_at = datetime(2026, 9, 4, tzinfo=timezone.utc)
    await writer.process_event(db_conn, _event("CatalogLatency", started_at))
    cursor = (await read_snapshot(db_conn)).stream_id
    await writer.process_event(db_conn, _event("CacheEvictions", started_at + timedelta(seconds=1)))

    deltas = await read_delta_events(db_conn, cursor)

    assert [event.event_type for event in deltas] == [
        "incident.upsert",
        "graph.edge.upsert",
        "card.update",
        "metrics.update",
    ]
    assert [event.stream_id for event in deltas] == sorted(event.stream_id for event in deltas)
    assert len({event.stream_id for event in deltas}) == 4
    assert deltas[1].data["edges"]
    assert all("streamId" in event.encode() for event in deltas)
