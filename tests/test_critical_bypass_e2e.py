import json
from datetime import datetime, timezone
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


def _event(service: str, alertname: str, message: str) -> NormalizedEvent:
    labels = {"environment": "production", "cluster": "critical-cluster"}
    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint=f"ingest-{alertname}",
        source="prometheus",
        service=service,
        alertname=alertname,
        severity_raw="warning",
        status="firing",
        labels=labels,
        message=message,
        fired_at=datetime.now(timezone.utc),
        raw_payload={"labels": labels},
    )


@pytest.mark.asyncio
async def test_payment_failure_bypass_is_atomic_and_hash_chained(db_conn) -> None:
    writer = DbWriter()
    event = _event("payment-api", "PaymentFailureRate", "payment capture failures")

    result = await writer.process_event(db_conn, event)

    assert result["bypassed"] is True
    async with db_conn.execute(
        "SELECT * FROM raw_events WHERE event_id = ?", (str(event.event_id),)
    ) as cursor:
        raw_event = await cursor.fetchone()
    async with db_conn.execute(
        "SELECT channel, action FROM delivery_intents WHERE incident_id = ?",
        (result["incident_id"],),
    ) as cursor:
        intents = await cursor.fetchall()
    canonical_payload = json.dumps(
        {
            "event": event.model_dump(mode="json"),
            "decision": json.loads(raw_event["decision_payload_json"]),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    assert raw_event["bypass_reason"] == "PAYMENT_FAILURE"
    assert compute_row_hash(raw_event["prev_hash"], canonical_payload) == raw_event["row_hash"]
    assert {(row["channel"], row["action"]) for row in intents} == {
        ("pagerduty", "trigger"),
        ("slack", "create"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service", "alertname", "message", "expected_reason"),
    [
        ("auth-service", "AuthenticationOutage", "authentication unavailable", "AUTH_OUTAGE"),
        ("storage-service", "DataLossDetected", "data loss detected", "DATA_LOSS"),
    ],
)
async def test_protected_domain_emergencies_bypass_writer_pipeline(
    db_conn, service: str, alertname: str, message: str, expected_reason: str
) -> None:
    result = await DbWriter().process_event(db_conn, _event(service, alertname, message))

    async with db_conn.execute(
        "SELECT bypassed, bypass_reason FROM raw_events WHERE incident_id = ?",
        (result["incident_id"],),
    ) as cursor:
        raw_event = await cursor.fetchone()

    assert result["bypassed"] is True
    assert raw_event["bypassed"] == 1
    assert raw_event["bypass_reason"] == expected_reason
