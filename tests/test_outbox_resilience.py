"""Delivery behaviour when a notification channel is down.

The headline case is the first test: before the circuit breaker existed, every
queued row was permanently dead-lettered about 62 seconds into a Slack outage,
because a channel-level failure was charged to the row that happened to be at
the head of the queue.
"""

import json
from datetime import datetime, timedelta, timezone
import aiosqlite
import httpx
import pytest
import pytest_asyncio

from src.db.connection import Database
from src.outbox import recovery
from src.outbox.channel_health import CLOSED, HALF_OPEN, OPEN, BreakerConfig
from src.outbox.failure_policy import (
    ChannelUnavailable,
    FailureKind,
    MessageRejected,
    RateLimited,
    classify,
    classify_slack_error,
)
from src.outbox.routing import priority_for, select_failover
from src.outbox.worker import OutboxWorker


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "resilience.db"))
    await database.connect()
    yield database
    await database.close()


async def _seed_incident(
    conn: aiosqlite.Connection,
    incident_id: str,
    *,
    severity: str = "critical",
    status: str = "ACKNOWLEDGED",
    alert_count: int = 1,
) -> None:
    now = _iso(datetime.now(timezone.utc))
    await conn.execute(
        """
        INSERT INTO incidents (
            incident_id, scope_key, stable_fingerprint, title, summary, severity,
            status, alert_count, first_alert_at, last_alert_at
        ) VALUES (?, 'prod/eu', ?, ?, 'summary text', ?, ?, ?, ?, ?)
        """,
        (
            incident_id,
            f"fp-{incident_id}",
            f"payment-api — Incident {incident_id}",
            severity,
            status,
            alert_count,
            now,
            now,
        ),
    )


async def _enqueue(
    conn: aiosqlite.Connection,
    incident_id: str,
    *,
    channel: str = "slack",
    action: str = "create",
    priority: int = 0,
) -> int:
    now = _iso(datetime.now(timezone.utc))
    cursor = await conn.execute(
        """
        INSERT INTO outbox (
            incident_id, channel, action, payload_json, status,
            next_attempt_at, priority, origin_channel
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            incident_id,
            channel,
            action,
            json.dumps({"incident_id": incident_id}),
            now,
            priority,
            channel,
        ),
    )
    await conn.commit()
    return cursor.lastrowid


async def _outbox_rows(conn: aiosqlite.Connection) -> list[dict]:
    async with conn.execute("SELECT * FROM outbox ORDER BY outbox_id") as cursor:
        return [dict(row) for row in await cursor.fetchall()]


# ── failure classification ────────────────────────────────────────────────


def test_transport_failure_is_a_channel_outage_not_a_bad_message():
    verdict = classify(httpx.ConnectError("dns failure"))
    assert verdict.kind is FailureKind.CHANNEL_DOWN
    assert verdict.trips_breaker
    assert not verdict.counts_against_attempts


def test_bad_payload_is_the_rows_own_fault():
    verdict = classify(MessageRejected("slack:invalid_blocks"))
    assert verdict.kind is FailureKind.MESSAGE_FATAL
    assert not verdict.trips_breaker


def test_rate_limit_is_not_an_outage_and_carries_the_providers_hint():
    verdict = classify(RateLimited(12.0))
    assert verdict.kind is FailureKind.TRANSIENT
    assert verdict.retry_after_seconds == 12.0
    assert not verdict.trips_breaker


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("invalid_blocks", FailureKind.MESSAGE_FATAL),
        ("channel_not_found", FailureKind.MESSAGE_FATAL),
        ("service_unavailable", FailureKind.CHANNEL_DOWN),
        ("token_revoked", FailureKind.CHANNEL_DOWN),
    ],
)
def test_slack_error_codes_split_by_blame(code, expected):
    assert classify_slack_error(code).kind is expected


# ── the regression this work exists for ───────────────────────────────────


@pytest.mark.asyncio
async def test_slack_outage_longer_than_the_retry_budget_does_not_kill_the_backlog(db):
    """A sustained outage must not dead-letter a single queued incident.

    With OUTBOX_MAX_ATTEMPTS=5 and exponential backoff, the old worker marked
    rows 'dead' roughly a minute into an outage. Here Slack fails far more
    times than that budget and every row must still be pending afterwards.
    """

    conn = db.writer_conn
    await _seed_incident(conn, "inc-outage")
    await _enqueue(conn, "inc-outage")

    worker = OutboxWorker(db, BreakerConfig(failure_threshold=2, probe_base_seconds=30))

    from src.outbox import worker as worker_module

    async def dead_slack(action, payload, external_ref):
        raise httpx.ConnectError("slack.com unreachable")

    async def accepting_pagerduty(action, payload, external_ref):
        return payload["incident_id"]

    originals = dict(worker_module._DISPATCHERS)
    worker_module._DISPATCHERS["slack"] = dead_slack
    worker_module._DISPATCHERS["pagerduty"] = accepting_pagerduty
    try:
        # Far more attempts than OUTBOX_MAX_ATTEMPTS. Dispatch is driven
        # directly so the run is not gated on wall-clock backoff.
        for _ in range(20):
            async with db.write_lock:
                async with conn.execute(
                    "SELECT * FROM outbox WHERE status = 'pending' AND channel = 'slack'"
                ) as cursor:
                    pending = [dict(row) for row in await cursor.fetchall()]
            for row in pending:
                await worker._dispatch(row)
    finally:
        worker_module._DISPATCHERS.update(originals)

    slack_rows = [row for row in await _outbox_rows(conn) if row["channel"] == "slack"]
    assert [row["status"] for row in slack_rows] == ["pending"]
    assert slack_rows[0]["attempts"] == 0, "an outage must not charge a row an attempt"

    state = await worker.health.get(conn, "slack")
    assert state.state == OPEN
    assert not state.is_available


@pytest.mark.asyncio
async def test_a_poison_payload_dead_letters_itself_without_stopping_the_channel(db):
    conn = db.writer_conn
    await _seed_incident(conn, "inc-poison")
    await _enqueue(conn, "inc-poison")

    worker = OutboxWorker(db, BreakerConfig(failure_threshold=2))

    async def rejecting_slack(action, payload, external_ref):
        raise MessageRejected("slack:invalid_blocks")

    from src.outbox import worker as worker_module

    original = worker_module._DISPATCHERS["slack"]
    worker_module._DISPATCHERS["slack"] = rejecting_slack
    try:
        for _ in range(5):
            await worker._poll_once()
    finally:
        worker_module._DISPATCHERS["slack"] = original

    rows = await _outbox_rows(conn)
    assert rows[0]["status"] == "dead"
    # One bad payload must never convince the breaker that Slack is down.
    assert (await worker.health.get(conn, "slack")).state == CLOSED


# ── breaker lifecycle ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_breaker_opens_only_after_repeated_channel_failures(db):
    conn = db.writer_conn
    worker = OutboxWorker(db, BreakerConfig(failure_threshold=3))
    verdict = classify(ChannelUnavailable("http:503"))

    state, opened = await worker.health.record_failure(conn, "slack", verdict)
    assert (state.state, opened) == (CLOSED, False)

    state, opened = await worker.health.record_failure(conn, "slack", verdict)
    assert (state.state, opened) == (CLOSED, False)

    state, opened = await worker.health.record_failure(conn, "slack", verdict)
    assert state.state == OPEN
    assert opened, "crossing the threshold is the moment the outage is declared"
    assert state.next_probe_at is not None


@pytest.mark.asyncio
async def test_probe_success_trials_the_channel_before_trusting_it(db):
    conn = db.writer_conn
    worker = OutboxWorker(db, BreakerConfig(failure_threshold=1))
    await worker.health.record_failure(conn, "slack", classify(httpx.ConnectError("x")))

    state = await worker.health.record_probe_result(conn, "slack", True, "probe_ok")
    assert state.state == HALF_OPEN, "a probe re-opens the door, it does not close the breaker"
    assert state.is_available

    # A real delivery failing during the trial sends us straight back down.
    state, _ = await worker.health.record_failure(
        conn, "slack", classify(httpx.ConnectError("still down"))
    )
    assert state.state == OPEN


@pytest.mark.asyncio
async def test_probe_backoff_grows_so_a_long_outage_is_cheap(db):
    conn = db.writer_conn
    worker = OutboxWorker(db, BreakerConfig(failure_threshold=1, probe_base_seconds=5))
    await worker.health.record_failure(conn, "slack", classify(httpx.ConnectError("x")))

    delays = []
    for _ in range(4):
        before = datetime.now(timezone.utc)
        state = await worker.health.record_probe_result(conn, "slack", False, "still down")
        delays.append((state.next_probe_at - before).total_seconds())

    assert delays == sorted(delays), "each failed probe must wait longer than the last"
    assert delays[-1] > delays[0]


@pytest.mark.asyncio
async def test_successful_delivery_closes_the_breaker_and_the_outage_record(db):
    conn = db.writer_conn
    worker = OutboxWorker(db, BreakerConfig(failure_threshold=1))
    await worker.health.record_failure(conn, "slack", classify(httpx.ConnectError("x")))

    state, recovered = await worker.health.record_success(conn, "slack")
    assert state.state == CLOSED
    assert recovered

    async with conn.execute(
        "SELECT detected_at, recovered_at FROM channel_outages WHERE channel = 'slack'"
    ) as cursor:
        outage = dict(await cursor.fetchone())
    assert outage["recovered_at"] is not None


# ── recovery: coalescing and rendering ────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_collapses_a_backlog_to_one_message_per_incident(db):
    conn = db.writer_conn
    await _seed_incident(conn, "inc-storm", alert_count=1)
    ids = [await _enqueue(conn, "inc-storm", action="create")]
    for _ in range(9):
        ids.append(await _enqueue(conn, "inc-storm", action="update"))
    await _enqueue(conn, "inc-other", action="create")

    collapsed = await recovery.coalesce_pending(conn, "slack")
    await conn.commit()

    assert collapsed == 9
    rows = await _outbox_rows(conn)
    pending = [row for row in rows if row["status"] == "pending"]
    assert len(pending) == 2, "one surviving intent per incident"
    survivor = next(row for row in pending if row["incident_id"] == "inc-storm")
    assert survivor["outbox_id"] == ids[-1], "the newest intent is the one that survives"
    assert all(
        row["superseded_by"] == ids[-1]
        for row in rows
        if row["status"] == "superseded"
    )


@pytest.mark.asyncio
async def test_a_queued_message_is_rendered_from_current_state_not_stale_payload(db):
    conn = db.writer_conn
    await _seed_incident(conn, "inc-late", alert_count=1, status="ACKNOWLEDGED")
    await _enqueue(conn, "inc-late", action="create")

    # The storm continues while the channel is down.
    await conn.execute(
        "UPDATE incidents SET alert_count = 412, status = 'RESOLVED' WHERE incident_id = ?",
        ("inc-late",),
    )
    await conn.commit()

    hydrated = await recovery.hydrate_payload(conn, "inc-late", {"incident_id": "inc-late"})
    assert hydrated["alert_count"] == 412
    assert hydrated["title"] == "payment-api — Incident inc-late"

    # An incident that opened and closed inside the outage must not be
    # announced as newly firing when the channel returns.
    assert recovery.effective_action("create", hydrated["state"]) == "resolve"


@pytest.mark.asyncio
async def test_recovery_digest_reports_the_gap(db):
    conn = db.writer_conn
    await _seed_incident(conn, "inc-digest", severity="critical", status="RESOLVED")
    await _enqueue(conn, "inc-digest")

    started = datetime.now(timezone.utc) - timedelta(minutes=30)
    digest = await recovery.build_recovery_digest(
        conn, "slack", started, datetime.now(timezone.utc)
    )

    assert digest is not None
    assert digest["duration_seconds"] >= 1790
    assert digest["incidents_touched"] == 1
    assert digest["critical_incidents"] == 1
    assert digest["resolved_during_outage"] == 1

    await recovery.enqueue_recovery_digest(conn, "slack", digest)
    await conn.commit()

    rows = await _outbox_rows(conn)
    digest_row = next(row for row in rows if row["priority"] == -1)
    assert json.loads(digest_row["payload_json"])["kind"] == "recovery_digest"


# ── failover ──────────────────────────────────────────────────────────────


def test_only_urgent_traffic_earns_a_fallback_channel():
    assert priority_for("critical") == 0
    assert priority_for("low") == 3
    assert select_failover("slack", "create", {"pagerduty", "email"}).channel == "pagerduty"
    # PagerDuty cannot edit a card, so an update has to become a trigger; its
    # dedup_key keeps that idempotent.
    assert select_failover("slack", "update", {"pagerduty"}).action == "create"
    assert select_failover("slack", "create", {"email"}).channel == "email"
    assert select_failover("slack", "create", set()) is None


@pytest.mark.asyncio
async def test_outage_reroutes_criticals_and_leaves_low_severity_queued(db):
    conn = db.writer_conn
    await _seed_incident(conn, "inc-crit", severity="critical")
    await _seed_incident(conn, "inc-low", severity="low")
    critical_id = await _enqueue(conn, "inc-crit", priority=priority_for("critical"))
    await _enqueue(conn, "inc-low", priority=priority_for("low"))

    worker = OutboxWorker(db, BreakerConfig(failure_threshold=1))
    await worker.health.record_failure(conn, "slack", classify(httpx.ConnectError("x")))
    await conn.commit()

    await worker._failover_pending("slack")

    rows = await _outbox_rows(conn)
    failover = [row for row in rows if row["failover_of"] is not None]
    assert len(failover) == 1, "only the critical is worth waking a second path for"
    assert failover[0]["channel"] == "pagerduty"
    assert failover[0]["failover_of"] == critical_id
    assert json.loads(failover[0]["payload_json"])["failover_from"] == "slack"

    # The Slack row survives, tagged so the eventual card cannot be mistaken
    # for a second, separate incident.
    original = next(row for row in rows if row["outbox_id"] == critical_id)
    assert original["status"] == "pending"
    assert json.loads(original["payload_json"])["delivered_via_fallback"] == "pagerduty"


# ── drain order ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backlog_drains_criticals_before_noise(db):
    conn = db.writer_conn
    delivered: list[str] = []

    for index, severity in enumerate(["low", "low", "critical", "medium"]):
        incident_id = f"inc-{index}-{severity}"
        await _seed_incident(conn, incident_id, severity=severity)
        await _enqueue(conn, incident_id, priority=priority_for(severity))

    worker = OutboxWorker(db)

    async def recording_slack(action, payload, external_ref):
        delivered.append(payload["incident_id"])
        return "ts-1"

    from src.outbox import worker as worker_module

    original = worker_module._DISPATCHERS["slack"]
    worker_module._DISPATCHERS["slack"] = recording_slack
    try:
        await worker._poll_once()
    finally:
        worker_module._DISPATCHERS["slack"] = original

    assert delivered[0] == "inc-2-critical"
    assert delivered[-2:] == ["inc-0-low", "inc-1-low"]


@pytest.mark.asyncio
async def test_updates_edit_the_incidents_existing_message(db):
    """One incident must stay one Slack message across its whole lifecycle."""

    conn = db.writer_conn
    await _seed_incident(conn, "inc-thread")
    await _enqueue(conn, "inc-thread", action="create")

    seen: list[tuple[str, str | None]] = []

    async def recording_slack(action, payload, external_ref):
        seen.append((action, external_ref))
        return "1712345678.000100"

    from src.outbox import worker as worker_module

    original = worker_module._DISPATCHERS["slack"]
    worker_module._DISPATCHERS["slack"] = recording_slack
    try:
        worker = OutboxWorker(db)
        await worker._poll_once()
        await _enqueue(conn, "inc-thread", action="update")
        await worker._poll_once()
    finally:
        worker_module._DISPATCHERS["slack"] = original

    assert seen[0] == ("create", None)
    # The second call carries the ts from the first, so Slack edits the card
    # instead of posting a second one.
    assert seen[1] == ("update", "1712345678.000100")
