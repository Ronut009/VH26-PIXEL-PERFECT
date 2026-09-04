from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio

from src.contracts import NormalizedEvent
from src.db.writer import DbWriter
from src.stubs import stub_process_incident, stub_update_graph

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


def _make_event(severity_raw: str = "warning", labels: dict | None = None) -> NormalizedEvent:
    base_labels = {"alertname": "HighCPUUsage", "service": "payment-api", "severity": severity_raw}
    if labels:
        base_labels.update(labels)

    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint="fp-test",
        source="prometheus",
        service="payment-api",
        alertname="HighCPUUsage",
        severity_raw=severity_raw,
        status="firing",
        labels=base_labels,
        message="CPU usage above 90%",
        fired_at=datetime.now(timezone.utc),
        raw_payload={"labels": base_labels},
    )


@pytest_asyncio.fixture
async def db_conn(tmp_path):
    db_path = tmp_path / "test_alerts.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield conn
    await conn.close()


@pytest.fixture
def writer():
    return DbWriter(process_incident_fn=stub_process_incident, update_graph_fn=stub_update_graph)


@pytest.mark.asyncio
async def test_happy_path_creates_raw_event_incident_and_outbox_rows(db_conn, writer):
    event = _make_event(severity_raw="warning")

    result = await writer.process_event(db_conn, event)

    async with db_conn.execute("SELECT * FROM raw_events WHERE event_id = ?", (str(event.event_id),)) as cur:
        raw_row = await cur.fetchone()
    assert raw_row is not None
    assert raw_row["bypassed"] == 0

    async with db_conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (result["incident_id"],)) as cur:
        incident_row = await cur.fetchone()
    assert incident_row is not None
    assert incident_row["severity"] == "medium"

    async with db_conn.execute("SELECT * FROM outbox WHERE incident_id = ?", (result["incident_id"],)) as cur:
        outbox_rows = await cur.fetchall()
    assert len(outbox_rows) == 1
    assert outbox_rows[0]["channel"] == "slack"
    assert outbox_rows[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_critical_bypass_skips_incident_engine_and_routes_to_pagerduty(db_conn, writer):
    event = _make_event(severity_raw="critical", labels={"severity": "critical"})

    result = await writer.process_event(db_conn, event)

    assert result["bypassed"] is True

    async with db_conn.execute("SELECT * FROM raw_events WHERE event_id = ?", (str(event.event_id),)) as cur:
        raw_row = await cur.fetchone()
    assert raw_row["bypassed"] == 1

    async with db_conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (result["incident_id"],)) as cur:
        incident_row = await cur.fetchone()
    assert incident_row["severity"] == "critical"

    async with db_conn.execute("SELECT * FROM outbox WHERE incident_id = ?", (result["incident_id"],)) as cur:
        outbox_rows = await cur.fetchall()
    assert len(outbox_rows) == 1
    assert outbox_rows[0]["channel"] == "pagerduty"
    assert outbox_rows[0]["action"] == "create"


@pytest.mark.asyncio
async def test_priority_p0_also_triggers_bypass(db_conn, writer):
    event = _make_event(severity_raw="warning", labels={"priority": "P0"})

    result = await writer.process_event(db_conn, event)

    assert result["bypassed"] is True

    async with db_conn.execute("SELECT * FROM outbox WHERE incident_id = ?", (result["incident_id"],)) as cur:
        outbox_rows = await cur.fetchall()
    assert outbox_rows[0]["channel"] == "pagerduty"


@pytest.mark.asyncio
async def test_hash_chain_advances_across_multiple_events(db_conn, writer):
    event1 = _make_event(severity_raw="warning")
    event2 = _make_event(severity_raw="warning")

    result1 = await writer.process_event(db_conn, event1)
    result2 = await writer.process_event(db_conn, event2)

    assert result2["seq"] == result1["seq"] + 1

    async with db_conn.execute(
        "SELECT prev_hash, row_hash FROM raw_events WHERE seq = ?", (result1["seq"],)
    ) as cur:
        row1 = await cur.fetchone()
    async with db_conn.execute(
        "SELECT prev_hash, row_hash FROM raw_events WHERE seq = ?", (result2["seq"],)
    ) as cur:
        row2 = await cur.fetchone()

    assert row2["prev_hash"] == row1["row_hash"]
