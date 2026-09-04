"""Two workers must not send the same message, and email must really send."""

import json
import smtplib
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from src.config import settings
from src.db.connection import Database
from src.outbox import email as email_channel
from src.outbox.failure_policy import ChannelUnavailable, FailureKind, classify
from src.outbox.worker import OutboxWorker


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "lease.db"))
    await database.connect()
    yield database
    await database.close()


async def _queue(conn: aiosqlite.Connection, channel: str = "slack") -> str:
    incident_id = str(uuid.uuid4())
    now = _iso(datetime.now(timezone.utc))
    await conn.execute(
        """
        INSERT INTO incidents (
            incident_id, scope_key, stable_fingerprint, title, summary, severity,
            status, alert_count, first_alert_at, last_alert_at
        ) VALUES (?, 'prod/eu', ?, 'svc - Alert', 'summary', 'high',
                  'ACKNOWLEDGED', 1, ?, ?)
        """,
        (incident_id, f"fp-{incident_id}", now, now),
    )
    await conn.execute(
        """
        INSERT INTO outbox (incident_id, channel, action, payload_json, status,
                            next_attempt_at, priority, origin_channel)
        VALUES (?, ?, 'create', ?, 'pending', ?, 1, ?)
        """,
        (
            incident_id,
            channel,
            json.dumps({"incident_id": incident_id}),
            now,
            channel,
        ),
    )
    await conn.commit()
    return incident_id


# ── leasing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_claimed_row_is_invisible_to_a_second_worker(db):
    """The property that makes a second worker safe to add at all."""

    conn = db.writer_conn
    for _ in range(3):
        await _queue(conn)

    first = OutboxWorker(db, worker_id="worker-a")
    second = OutboxWorker(db, worker_id="worker-b")
    now = _iso(datetime.now(timezone.utc))

    claimed_by_a = await first._claim(conn, {"slack"}, now)
    claimed_by_b = await second._claim(conn, {"slack"}, now)

    assert len(claimed_by_a) == 3
    assert claimed_by_b == [], "a leased row must not be claimable twice"

    ids_a = {row["outbox_id"] for row in claimed_by_a}
    ids_b = {row["outbox_id"] for row in claimed_by_b}
    assert ids_a.isdisjoint(ids_b)


@pytest.mark.asyncio
async def test_two_workers_split_the_queue_rather_than_duplicating_it(db):
    conn = db.writer_conn
    for _ in range(6):
        await _queue(conn)

    now = _iso(datetime.now(timezone.utc))
    # A small batch size forces the split to be observable.
    original_batch = settings.OUTBOX_BATCH_SIZE
    settings.OUTBOX_BATCH_SIZE = 3
    try:
        a = await OutboxWorker(db, worker_id="worker-a")._claim(conn, {"slack"}, now)
        b = await OutboxWorker(db, worker_id="worker-b")._claim(conn, {"slack"}, now)
    finally:
        settings.OUTBOX_BATCH_SIZE = original_batch

    assert len(a) == 3 and len(b) == 3
    assert {r["outbox_id"] for r in a}.isdisjoint({r["outbox_id"] for r in b})


@pytest.mark.asyncio
async def test_an_expired_lease_is_reclaimed_so_a_dead_worker_strands_nothing(db):
    conn = db.writer_conn
    await _queue(conn)

    # A worker claimed this row and then died without releasing it.
    stale = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    await conn.execute(
        "UPDATE outbox SET locked_by = 'dead-worker', locked_until = ?", (stale,)
    )
    await conn.commit()

    now = _iso(datetime.now(timezone.utc))
    reclaimed = await OutboxWorker(db, worker_id="worker-live")._claim(
        conn, {"slack"}, now
    )

    assert len(reclaimed) == 1, "an expired lease must not strand the row forever"


@pytest.mark.asyncio
async def test_delivery_releases_the_lease(db):
    conn = db.writer_conn
    await _queue(conn)

    worker = OutboxWorker(db, worker_id="worker-a")

    async def accepting(action, payload, external_ref):
        return "ts-1"

    from src.outbox import worker as worker_module

    original = worker_module._DISPATCHERS["slack"]
    worker_module._DISPATCHERS["slack"] = accepting
    try:
        await worker._poll_once()
    finally:
        worker_module._DISPATCHERS["slack"] = original

    async with conn.execute(
        "SELECT status, locked_by, locked_until FROM outbox"
    ) as cursor:
        row = dict(await cursor.fetchone())

    assert row["status"] == "sent"
    assert row["locked_by"] is None and row["locked_until"] is None


# ── email channel ─────────────────────────────────────────────────────────


def test_an_unconfigured_relay_is_a_channel_problem_not_a_bad_message():
    """Misconfiguration must not dead-letter the mail it could not send.

    Treating "no SMTP host" as this row's fault would burn its retry budget and
    silently discard every fallback email, which is the exact failure mode the
    breaker work removed for Slack.
    """

    verdict = classify(ChannelUnavailable("smtp_not_configured"))
    assert verdict.kind is FailureKind.CHANNEL_DOWN
    assert not verdict.counts_against_attempts


def test_recipients_are_parsed_from_a_comma_separated_list(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_TO", "a@x.com, b@x.com ,, c@x.com")
    assert email_channel.recipients() == ["a@x.com", "b@x.com", "c@x.com"]


def test_the_subject_line_leads_with_what_matters():
    assert email_channel._subject(
        {"severity": "critical", "title": "db-primary down"}
    ) == "[CRITICAL] db-primary down"
    # A resolution should not look like a new emergency in an inbox.
    assert email_channel._subject(
        {"severity": "critical", "title": "db-primary down", "state": "RESOLVED"}
    ) == "[RESOLVED] db-primary down"


def test_the_body_explains_why_email_was_used_at_all():
    body = email_channel._body(
        {
            "title": "db-primary down",
            "summary": "connections exhausted",
            "severity": "critical",
            "state": "ACKNOWLEDGED",
            "alert_count": 517,
            "incident_id": "abc",
            "failover_from": "slack",
        }
    )
    assert "517" in body
    assert "slack" in body
    assert "not a separate incident" in body


def test_an_inferred_resolution_says_so_in_the_mail_too():
    body = email_channel._body(
        {
            "title": "api down",
            "state": "RESOLVED",
            "resolution_source": "inferred_silence",
            "incident_id": "abc",
        }
    )
    assert "Not confirmed by a human" in body


def test_a_storm_email_lists_the_whole_cascade():
    body = email_channel._body(
        {
            "title": "db-primary (+2 correlated)",
            "incident_id": "abc",
            "group": {
                "member_count": 3,
                "total_alert_count": 517,
                "members": [
                    {"title": "db-primary", "alert_count": 12, "status": "ACKNOWLEDGED"},
                    {"title": "api-gateway", "alert_count": 300, "status": "ACKNOWLEDGED"},
                    {"title": "pod-restarts", "alert_count": 205, "status": "ACKNOWLEDGED"},
                ],
            },
        }
    )
    assert "Correlated storm: 3 incidents" in body
    assert "api-gateway" in body


def test_messages_about_one_incident_thread_together(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_FROM", "alerts@example.com")
    message = email_channel._build_message(
        {"incident_id": "inc-42", "title": "x"}, ["oncall@example.com"]
    )
    assert message["References"] == "<incident-inc-42@pulsegraph.local>"
    assert message["In-Reply-To"] == message["References"]


@pytest.mark.asyncio
async def test_smtp_failures_map_to_the_right_blame(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "EMAIL_FROM", "alerts@example.com")
    monkeypatch.setattr(settings, "EMAIL_TO", "oncall@example.com")

    class UnreachableSMTP:
        def __init__(self, *args, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", UnreachableSMTP)

    with pytest.raises(ChannelUnavailable):
        await email_channel.send("create", {"incident_id": "abc", "title": "x"}, None)


@pytest.mark.asyncio
async def test_a_send_that_works_returns_a_threadable_reference(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "EMAIL_FROM", "alerts@example.com")
    monkeypatch.setattr(settings, "EMAIL_TO", "oncall@example.com")
    monkeypatch.setattr(settings, "SMTP_USERNAME", "")

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host

        def starttls(self, context=None):
            sent["tls"] = True

        def send_message(self, message, to_addrs=None):
            sent["to"] = to_addrs
            sent["subject"] = message["Subject"]

        def quit(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    ref = await email_channel.send(
        "create", {"incident_id": "abc", "title": "db down", "severity": "high"}, None
    )

    assert sent["to"] == ["oncall@example.com"]
    assert sent["subject"] == "[HIGH] db down"
    assert sent["tls"] is True
    assert ref.startswith("<") and "pulsegraph.local" in ref
