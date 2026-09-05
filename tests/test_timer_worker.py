import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from src.contracts import NormalizedEvent
from src.db.connection import Database
from src.db.writer import DbWriter
from src.engine.timer_wheel import TimerWheel
from src.engine.timer_worker import TimerWorker

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


@pytest_asyncio.fixture
async def timer_components(tmp_path):
    database = Database(str(tmp_path / "timer-worker.db"))
    await database.connect()
    assert database.writer_conn is not None
    await database.writer_conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    wheel = TimerWheel()
    worker = TimerWorker(database, wheel, poll_interval_seconds=0.01)
    writer = DbWriter(timer_wheel=wheel)
    yield database, wheel, worker, writer

    await worker.stop()
    await database.close()


def _event() -> NormalizedEvent:
    labels = {
        "environment": "production",
        "cluster": "timer-cluster",
        "pod": "api-a",
        "pod_uid": "api-a-uid",
    }
    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint="ingest-timer",
        source="prometheus",
        service="api",
        alertname="HighLatency",
        severity_raw="warning",
        status="firing",
        labels=labels,
        message="Latency is elevated",
        fired_at=datetime.now(timezone.utc),
        raw_payload={"labels": labels},
    )


async def _wait_for_state(database: Database, incident_id: str, state: str) -> None:
    assert database.writer_conn is not None
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        async with database.writer_conn.execute(
            "SELECT status FROM incidents WHERE incident_id = ?", (incident_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None and row["status"] == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"incident {incident_id} did not reach {state}")


@pytest.mark.asyncio
async def test_timer_worker_persists_due_deadline_as_quiescent(timer_components) -> None:
    database, wheel, worker, writer = timer_components
    assert database.writer_conn is not None
    result = await writer.process_event(database.writer_conn, _event())
    incident_id = result["incident_id"]

    wheel.schedule(UUID(incident_id), int(time.time() * 1000) + 25)
    worker.start()
    await _wait_for_state(database, incident_id, "QUIESCENT")

    async with database.writer_conn.execute(
        "SELECT quiet_at_ms FROM incidents WHERE incident_id = ?", (incident_id,)
    ) as cursor:
        incident = await cursor.fetchone()
    async with database.writer_conn.execute(
        "SELECT decision_payload_json FROM raw_events WHERE incident_id = ? ORDER BY seq DESC LIMIT 1",
        (incident_id,),
    ) as cursor:
        lifecycle_event = await cursor.fetchone()
    async with database.writer_conn.execute(
        "SELECT channel, action FROM delivery_intents WHERE incident_id = ? ORDER BY delivery_intent_id DESC LIMIT 1",
        (incident_id,),
    ) as cursor:
        card_intent = await cursor.fetchone()

    assert incident["quiet_at_ms"] is None
    assert json.loads(lifecycle_event["decision_payload_json"])["trigger"] == "QUIET_DEADLINE"
    assert (card_intent["channel"], card_intent["action"]) == ("slack", "update")


@pytest.mark.asyncio
async def test_timer_worker_ignores_deadline_for_non_acknowledged_incident(timer_components) -> None:
    database, wheel, worker, writer = timer_components
    assert database.writer_conn is not None
    result = await writer.process_event(database.writer_conn, _event())
    incident_id = result["incident_id"]
    await database.writer_conn.execute(
        "UPDATE incidents SET status = 'OPEN' WHERE incident_id = ?", (incident_id,)
    )
    await database.writer_conn.commit()

    wheel.schedule(UUID(incident_id), int(time.time() * 1000) + 25)
    worker.start()
    await asyncio.sleep(0.1)

    async with database.writer_conn.execute(
        "SELECT status FROM incidents WHERE incident_id = ?", (incident_id,)
    ) as cursor:
        incident = await cursor.fetchone()
    async with database.writer_conn.execute(
        "SELECT COUNT(*) AS count FROM raw_events WHERE incident_id = ?", (incident_id,)
    ) as cursor:
        event_count = (await cursor.fetchone())["count"]

    assert incident["status"] == "OPEN"
    assert event_count == 1
