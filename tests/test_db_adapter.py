import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio

from src.contracts import NormalizedEvent
from src.db.hashchain import compute_row_hash
from src.db.writer import DbWriter

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


@pytest_asyncio.fixture
async def db_conn() -> aiosqlite.Connection:
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield connection
    await connection.close()


def _event(
    *,
    event_id=None,
    fired_at: datetime | None = None,
    severity_raw: str = "warning",
    labels: dict[str, str] | None = None,
) -> NormalizedEvent:
    event_labels = {
        "environment": "production",
        "cluster": "payments",
        "pod": "payment-api-a",
        "pod_uid": "pod-a",
    }
    if labels:
        event_labels.update(labels)
    return NormalizedEvent(
        event_id=event_id or uuid4(),
        fingerprint="ingest-fingerprint",
        source="prometheus",
        service="payment-api",
        alertname="HighCPUUsage",
        severity_raw=severity_raw,
        status="firing",
        labels=event_labels,
        message="CPU above threshold",
        fired_at=fired_at or datetime(2026, 9, 4, tzinfo=timezone.utc),
        raw_payload={"labels": event_labels},
    )


@pytest.mark.asyncio
async def test_duplicate_alert_updates_existing_incident(db_conn) -> None:
    writer = DbWriter()
    first = _event()
    second = _event(
        fired_at=first.fired_at + timedelta(milliseconds=100),
        labels={"pod": "payment-api-b", "pod_uid": "pod-b"},
    )

    first_result = await writer.process_event(db_conn, first)
    second_result = await writer.process_event(db_conn, second)

    assert second_result["incident_id"] == first_result["incident_id"]
    assert second_result["is_duplicate"] is True

    async with db_conn.execute("SELECT COUNT(*) AS count FROM incidents") as cursor:
        incident_count = (await cursor.fetchone())["count"]
    async with db_conn.execute(
        "SELECT alert_count, status FROM incidents WHERE incident_id = ?",
        (first_result["incident_id"],),
    ) as cursor:
        incident = await cursor.fetchone()
    async with db_conn.execute("SELECT COUNT(*) AS count FROM raw_events") as cursor:
        event_count = (await cursor.fetchone())["count"]

    assert incident_count == 1
    assert event_count == 2
    assert incident["alert_count"] == 2
    assert incident["status"] == "ACKNOWLEDGED"


@pytest.mark.asyncio
async def test_critical_bypass_persists_audit_fields(db_conn) -> None:
    writer = DbWriter()
    event = _event(severity_raw="critical")

    result = await writer.process_event(db_conn, event)

    assert result["bypassed"] is True
    async with db_conn.execute(
        """
        SELECT bypassed, bypass_reason, decision_payload_json
        FROM raw_events
        WHERE event_id = ?
        """,
        (str(event.event_id),),
    ) as cursor:
        raw_event = await cursor.fetchone()
    async with db_conn.execute(
        "SELECT channel, action FROM delivery_intents WHERE incident_id = ? ORDER BY channel",
        (result["incident_id"],),
    ) as cursor:
        intents = await cursor.fetchall()

    assert raw_event["bypassed"] == 1
    assert raw_event["bypass_reason"] == "SEVERITY_CRITICAL"
    assert json.loads(raw_event["decision_payload_json"])["bypass_reason"] == "SEVERITY_CRITICAL"
    assert {(row["channel"], row["action"]) for row in intents} == {
        ("pagerduty", "trigger"),
        ("slack", "create"),
    }


@pytest.mark.asyncio
async def test_hash_chain_covers_critical_audit_payload(db_conn) -> None:
    writer = DbWriter()
    event = _event(severity_raw="critical")
    await writer.process_event(db_conn, event)

    async with db_conn.execute("SELECT * FROM raw_events WHERE event_id = ?", (str(event.event_id),)) as cursor:
        row = await cursor.fetchone()

    canonical_payload = json.dumps(
        {
            "event": event.model_dump(mode="json"),
            "decision": json.loads(row["decision_payload_json"]),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert compute_row_hash(row["prev_hash"], canonical_payload) == row["row_hash"]

    tampered_payload = json.dumps(
        {
            "event": event.model_dump(mode="json"),
            "decision": {**json.loads(row["decision_payload_json"]), "bypass_reason": "TAMPERED"},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert compute_row_hash(row["prev_hash"], tampered_payload) != row["row_hash"]
