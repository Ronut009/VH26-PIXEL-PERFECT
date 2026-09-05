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


@pytest.mark.asyncio
async def test_resolving_a_bypassed_incident_closes_it_instead_of_paging_again(
    db_conn,
) -> None:
    """A resolution must never take the bypass.

    The bypass branch fired on any classified event, so a `resolved` alert for
    a critical incident minted a *second* incident in ACKNOWLEDGED and paged
    again, while the original stayed open forever with no resolve ever
    delivered. In production that is a PagerDuty incident nobody can close from
    the monitoring side.
    """

    writer = DbWriter()
    firing = _event("payment-api", "PaymentFailureRate", "payment capture failures")
    opened = await writer.process_event(db_conn, firing)

    resolved_event = firing.model_copy(
        update={"event_id": uuid4(), "status": "resolved", "message": "payments recovered"}
    )
    closed = await writer.process_event(db_conn, resolved_event)

    assert opened["bypassed"] is True, "the firing alert still pages"
    assert closed["bypassed"] is False, "the resolution must not page"
    assert closed["incident_id"] == opened["incident_id"], (
        "the resolution must close the incident that was opened, not mint a new one"
    )

    async with db_conn.execute("SELECT COUNT(*) AS count FROM incidents") as cursor:
        assert (await cursor.fetchone())["count"] == 1

    async with db_conn.execute(
        "SELECT status FROM incidents WHERE incident_id = ?", (opened["incident_id"],)
    ) as cursor:
        assert (await cursor.fetchone())["status"] == "RESOLVED"


@pytest.mark.asyncio
async def test_closing_a_paged_incident_resolves_the_page(db_conn) -> None:
    """A page opened by the bypass must be closed on the same channel.

    Only the bypass ever sent a PagerDuty trigger, and every later update and
    resolution went to Slack alone - so the page stayed open after the incident
    closed and could only be cleared by hand in PagerDuty. On exactly the
    alerts that matter most.
    """

    writer = DbWriter()
    firing = _event("payment-api", "PaymentFailureRate", "payment capture failures")
    opened = await writer.process_event(db_conn, firing)

    resolved_event = firing.model_copy(
        update={"event_id": uuid4(), "status": "resolved", "message": "payments recovered"}
    )
    await writer.process_event(db_conn, resolved_event)

    async with db_conn.execute(
        "SELECT channel, action FROM delivery_intents WHERE incident_id = ?",
        (opened["incident_id"],),
    ) as cursor:
        intents = {(row["channel"], row["action"]) for row in await cursor.fetchall()}

    assert ("pagerduty", "trigger") in intents, "the firing alert opens a page"
    assert ("pagerduty", "resolve") in intents, "and closing the incident closes it"


@pytest.mark.asyncio
async def test_a_slack_routed_incident_never_sends_a_pagerduty_resolve(db_conn) -> None:
    """No page was opened, so there is nothing to close."""

    writer = DbWriter()
    firing = _event("orders-api", "OrderQueueLatency", "order queue latency is climbing")
    opened = await writer.process_event(db_conn, firing)

    resolved_event = firing.model_copy(
        update={"event_id": uuid4(), "status": "resolved", "message": "latency recovered"}
    )
    await writer.process_event(db_conn, resolved_event)

    async with db_conn.execute(
        "SELECT channel FROM delivery_intents WHERE incident_id = ?",
        (opened["incident_id"],),
    ) as cursor:
        channels = {row["channel"] for row in await cursor.fetchall()}

    assert opened["bypassed"] is False
    assert channels == {"slack"}


@pytest.mark.asyncio
async def test_a_repeated_resolution_does_not_resolve_the_page_twice(db_conn) -> None:
    """The resolve fires on the transition, not on every resolved alert."""

    writer = DbWriter()
    firing = _event("payment-api", "PaymentFailureRate", "payment capture failures")
    opened = await writer.process_event(db_conn, firing)

    for _ in range(3):
        await writer.process_event(
            db_conn,
            firing.model_copy(
                update={"event_id": uuid4(), "status": "resolved", "message": "recovered"}
            ),
        )

    async with db_conn.execute(
        """
        SELECT COUNT(*) AS count FROM delivery_intents
        WHERE incident_id = ? AND channel = 'pagerduty' AND action = 'resolve'
        """,
        (opened["incident_id"],),
    ) as cursor:
        assert (await cursor.fetchone())["count"] == 1
