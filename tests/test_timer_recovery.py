"""Restart recovery coverage for persisted adaptive quiet deadlines."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import time
from uuid import UUID, uuid4

import pytest

from src.contracts import NormalizedEvent
from src.db.connection import Database
from src.db.writer import DbWriter
from src.engine.timer_wheel import TimerWheel
from src.engine.timer_worker import TimerWorker

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


def _event(*, alertname: str = "HighLatency") -> NormalizedEvent:
    labels = {
        "environment": "production",
        "cluster": "recovery-cluster",
        "pod": "api-a",
        "pod_uid": "volatile-pod-uid",
    }
    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint=f"ingest-recovery-{alertname}",
        source="prometheus",
        service="api",
        alertname=alertname,
        severity_raw="warning",
        status="firing",
        labels=labels,
        message="Latency is elevated",
        fired_at=datetime.now(timezone.utc),
        raw_payload={"labels": labels},
    )


async def _open_database(path: Path, *, initialize_schema: bool = False) -> Database:
    database = Database(str(path))
    await database.connect()
    assert database.writer_conn is not None
    if initialize_schema:
        await database.writer_conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return database


async def _create_incident(
    database: Database,
    *,
    deadline_ms: int,
    status: str = "ACKNOWLEDGED",
    alertname: str = "HighLatency",
) -> str:
    assert database.writer_conn is not None
    writer = DbWriter()
    result = await writer.process_event(database.writer_conn, _event(alertname=alertname))
    incident_id = result["incident_id"]
    await database.writer_conn.execute(
        "UPDATE incidents SET status = ?, quiet_at_ms = ? WHERE incident_id = ?",
        (status, deadline_ms, incident_id),
    )
    await database.writer_conn.commit()
    return str(incident_id)


async def _wait_for_status(database: Database, incident_id: str, status: str) -> None:
    assert database.writer_conn is not None
    timeout_at = time.monotonic() + 1.0
    while time.monotonic() < timeout_at:
        async with database.writer_conn.execute(
            "SELECT status FROM incidents WHERE incident_id = ?", (incident_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None and row["status"] == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"incident {incident_id} did not reach {status}")


@pytest.mark.asyncio
async def test_startup_recovery_reschedules_and_fires_persisted_deadline(tmp_path) -> None:
    """A persisted ACKNOWLEDGED deadline survives a complete database reconnect."""

    path = tmp_path / "restart-recovery.db"
    first_database = await _open_database(path, initialize_schema=True)
    deadline_ms = int(time.time() * 1000) + 100
    incident_id = await _create_incident(first_database, deadline_ms=deadline_ms)
    await first_database.close()

    restarted_database = await _open_database(path)
    wheel = TimerWheel()
    worker = TimerWorker(restarted_database, wheel, poll_interval_seconds=0.01)
    try:
        assert await worker.recover_persisted_deadlines() == 1
        assert wheel.next_deadline_ms() == deadline_ms

        worker.start()
        await _wait_for_status(restarted_database, incident_id, "QUIESCENT")

        assert wheel.next_deadline_ms() is None
    finally:
        await worker.stop()
        await restarted_database.close()


@pytest.mark.asyncio
async def test_recovery_skips_expired_deadline(tmp_path) -> None:
    database = await _open_database(tmp_path / "expired.db", initialize_schema=True)
    try:
        await _create_incident(
            database, deadline_ms=int(time.time() * 1000) - 1, alertname="Expired"
        )
        wheel = TimerWheel()
        worker = TimerWorker(database, wheel)

        assert await worker.recover_persisted_deadlines() == 0
        assert wheel.next_deadline_ms() is None
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["OPEN", "QUIESCENT", "RESOLVED"])
async def test_recovery_skips_non_acknowledged_incidents(tmp_path, status: str) -> None:
    database = await _open_database(tmp_path / f"{status.lower()}.db", initialize_schema=True)
    try:
        await _create_incident(
            database,
            deadline_ms=int(time.time() * 1000) + 60_000,
            status=status,
            alertname=status,
        )
        wheel = TimerWheel()
        worker = TimerWorker(database, wheel)

        assert await worker.recover_persisted_deadlines() == 0
        assert wheel.next_deadline_ms() is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_recovery_schedules_multiple_deadlines_in_time_order(tmp_path) -> None:
    database = await _open_database(tmp_path / "multiple.db", initialize_schema=True)
    now_ms = int(time.time() * 1000)
    try:
        first_id = await _create_incident(
            database, deadline_ms=now_ms + 5_000, alertname="First"
        )
        second_id = await _create_incident(
            database, deadline_ms=now_ms + 2_000, alertname="Second"
        )
        wheel = TimerWheel()
        worker = TimerWorker(database, wheel)

        assert await worker.recover_persisted_deadlines(now_ms=now_ms) == 2
        assert wheel.next_deadline_ms() == now_ms + 2_000
        assert [trigger.incident_id for trigger in wheel.pop_due(now_ms + 5_000)] == [
            UUID(second_id),
            UUID(first_id),
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_recovery_logs_recovered_count(tmp_path, caplog) -> None:
    database = await _open_database(tmp_path / "logged.db", initialize_schema=True)
    try:
        await _create_incident(
            database, deadline_ms=int(time.time() * 1000) + 60_000, alertname="Logged"
        )
        worker = TimerWorker(database, TimerWheel())

        with caplog.at_level("INFO", logger="src.engine.timer_worker"):
            assert await worker.recover_persisted_deadlines() == 1

        assert "timer_deadlines_recovered recovered_count=1" in caplog.text
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_recovery_is_idempotent_for_an_existing_queue_entry(tmp_path) -> None:
    database = await _open_database(tmp_path / "idempotent.db", initialize_schema=True)
    try:
        deadline_ms = int(time.time() * 1000) + 60_000
        await _create_incident(database, deadline_ms=deadline_ms, alertname="Idempotent")
        wheel = TimerWheel()
        worker = TimerWorker(database, wheel)

        assert await worker.recover_persisted_deadlines() == 1
        assert await worker.recover_persisted_deadlines() == 1
        assert len(wheel) == 1
        assert wheel.next_deadline_ms() == deadline_ms
    finally:
        await database.close()
