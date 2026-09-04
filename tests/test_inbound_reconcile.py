"""State flowing back in: callbacks, and inferring a fix nobody reported.

Three ways a fix reaches this system, one test group each:

    1. the monitor sends `resolved`      -> the lifecycle must accept it
    2. a human acts in Slack/PagerDuty   -> signed callbacks
    3. nobody says anything              -> silence-based inference
"""

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from src.db.connection import Database
from src.engine.silence_sweeper import (
    INFERRED_SILENCE,
    SilenceSweeper,
    silence_threshold_ms,
)
from src.inbound import reconcile
from src.inbound.reconcile import ACKNOWLEDGE, RESOLVE, ExternalAction
from src.inbound.signatures import (
    SignatureError,
    verify_pagerduty,
    verify_slack,
)
from src.outbox import recovery


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _id(label: str) -> str:
    """Stable UUID per readable label - real incident ids are UUIDs."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, label))


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "inbound.db"))
    await database.connect()
    yield database
    await database.close()


async def _seed(
    conn: aiosqlite.Connection,
    label: str,
    *,
    status: str = "ACKNOWLEDGED",
    severity: str = "high",
    last_alert_at: datetime | None = None,
    mean_gap: float = 5_000.0,
) -> str:
    moment = last_alert_at or datetime.now(timezone.utc)
    incident_id = _id(label)
    await conn.execute(
        """
        INSERT INTO incidents (
            incident_id, scope_key, stable_fingerprint, title, summary, severity,
            status, alert_count, first_alert_at, last_alert_at,
            ewma_mean_gap, ewma_variance, gap_history_json
        ) VALUES (?, 'prod/eu', ?, ?, 'summary', ?, ?, 3, ?, ?, ?, 0.0, '[]')
        """,
        (
            incident_id,
            f"fp-{label}",
            f"payment-api - {label}",
            severity,
            status,
            _iso(moment),
            _iso(moment),
            mean_gap,
        ),
    )
    await conn.commit()
    return incident_id


async def _status(conn: aiosqlite.Connection, incident_id: str) -> dict:
    async with conn.execute(
        """
        SELECT status, acknowledged_by, acknowledged_via, resolved_via,
               resolution_source, resolution_detail
        FROM incidents WHERE incident_id = ?
        """,
        (incident_id,),
    ) as cursor:
        return dict(await cursor.fetchone())


# ── 1. signature verification ─────────────────────────────────────────────


def test_slack_signature_accepts_a_genuine_request():
    secret, body = "s3cr3t", b"payload=%7B%7D"
    timestamp = str(int(time.time()))
    expected = (
        "v0="
        + hmac.new(
            secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
    )
    verify_slack(secret, timestamp, expected, body)


def test_slack_signature_rejects_tampering_and_replays():
    secret, body = "s3cr3t", b"payload=%7B%7D"
    timestamp = str(int(time.time()))
    good = (
        "v0="
        + hmac.new(
            secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
    )

    with pytest.raises(SignatureError):
        verify_slack(secret, timestamp, good, body + b"tampered")
    with pytest.raises(SignatureError):
        verify_slack("wrong-secret", timestamp, good, body)
    # An old capture must not be replayable even with a valid signature.
    old = str(int(time.time()) - 3600)
    replay = (
        "v0="
        + hmac.new(
            secret.encode(), b"v0:" + old.encode() + b":" + body, hashlib.sha256
        ).hexdigest()
    )
    with pytest.raises(SignatureError, match="replay window"):
        verify_slack(secret, old, replay, body)
    # An unset secret must fail closed, never open.
    with pytest.raises(SignatureError):
        verify_slack("", timestamp, good, body)


def test_pagerduty_signature_supports_secret_rotation():
    body = b'{"event":{}}'
    current = hmac.new(b"new-secret", body, hashlib.sha256).hexdigest()
    stale = hmac.new(b"old-secret", body, hashlib.sha256).hexdigest()

    # During rotation PagerDuty sends both; matching either is correct.
    verify_pagerduty("new-secret", f"v1={stale},v1={current}", body)
    with pytest.raises(SignatureError):
        verify_pagerduty("unrelated-secret", f"v1={current}", body)


# ── 2. human action reaches the incident ──────────────────────────────────


@pytest.mark.asyncio
async def test_pagerduty_acknowledgement_reaches_the_incident(db):
    conn = db.writer_conn
    incident = await _seed(conn, "inc-ack", status="OPEN")

    result = await reconcile.apply_external_action(
        conn,
        ExternalAction(
            inbound_id="pagerduty:evt-1",
            provider="pagerduty",
            kind=ACKNOWLEDGE,
            incident_id=incident,
            actor="dana@example.com",
        ),
    )
    await conn.commit()

    assert result.status == "applied"
    assert (result.from_state, result.to_state) == ("OPEN", "ACKNOWLEDGED")

    row = await _status(conn, incident)
    assert row["status"] == "ACKNOWLEDGED"
    assert row["acknowledged_by"] == "dana@example.com"
    assert row["acknowledged_via"] == "pagerduty"


@pytest.mark.asyncio
async def test_acting_in_one_channel_updates_the_other(db):
    """Acknowledging in PagerDuty has to move the Slack card, or the two
    surfaces disagree and responders trust neither."""

    conn = db.writer_conn
    incident = await _seed(conn, "inc-sync", status="OPEN")

    await reconcile.apply_external_action(
        conn,
        ExternalAction(
            inbound_id="pagerduty:evt-2",
            provider="pagerduty",
            kind=ACKNOWLEDGE,
            incident_id=incident,
            actor="dana",
        ),
    )
    await conn.commit()

    async with conn.execute(
        "SELECT channel FROM outbox WHERE incident_id = ?", (incident,)
    ) as cursor:
        channels = {row["channel"] for row in await cursor.fetchall()}

    assert channels == {"slack"}, "notify the other surface, not the one acted in"


@pytest.mark.asyncio
async def test_a_replayed_callback_changes_nothing(db):
    conn = db.writer_conn
    incident = await _seed(conn, "inc-replay", status="OPEN")
    action = ExternalAction(
        inbound_id="slack:trigger-9",
        provider="slack",
        kind=ACKNOWLEDGE,
        incident_id=incident,
        actor="ravi",
    )

    first = await reconcile.apply_external_action(conn, action)
    second = await reconcile.apply_external_action(conn, action)
    await conn.commit()

    assert first.status == "applied"
    assert second.status == "duplicate"

    async with conn.execute(
        "SELECT COUNT(*) AS n FROM raw_events WHERE incident_id = ?", (incident,)
    ) as cursor:
        assert (await cursor.fetchone())["n"] == 1, "no second ledger entry"


@pytest.mark.asyncio
async def test_a_second_responder_acknowledging_is_a_no_op_not_an_error(db):
    conn = db.writer_conn
    incident = await _seed(conn, "inc-double", status="ACKNOWLEDGED")

    result = await reconcile.apply_external_action(
        conn,
        ExternalAction(
            inbound_id="slack:trigger-10",
            provider="slack",
            kind=ACKNOWLEDGE,
            incident_id=incident,
        ),
    )
    await conn.commit()

    assert result.status == "ignored"
    assert result.detail == "no-op"


@pytest.mark.asyncio
async def test_an_unknown_incident_is_recorded_and_dropped(db):
    conn = db.writer_conn
    result = await reconcile.apply_external_action(
        conn,
        ExternalAction(
            inbound_id="slack:trigger-11",
            provider="slack",
            kind=ACKNOWLEDGE,
            incident_id="does-not-exist",
        ),
    )
    await conn.commit()

    assert result.status == "ignored"
    async with conn.execute(
        "SELECT status FROM inbound_events WHERE inbound_id = 'slack:trigger-11'"
    ) as cursor:
        assert (await cursor.fetchone())["status"] == "ignored"


@pytest.mark.asyncio
async def test_a_pagerduty_incident_maps_back_through_the_outbox(db):
    """PagerDuty may omit dedup_key, so the outbox is the identity map."""

    conn = db.writer_conn
    incident = await _seed(conn, "inc-map")
    await conn.execute(
        """
        INSERT INTO outbox (incident_id, channel, action, payload_json, status,
                            next_attempt_at, external_ref)
        VALUES (?, 'pagerduty', 'create', '{}', 'sent', '', 'PD-XYZ')
        """,
        (incident,),
    )
    await conn.commit()

    found = await reconcile.resolve_incident_id_from_ref(conn, "pagerduty", "PD-XYZ")
    assert found == incident
    assert await reconcile.resolve_incident_id_from_ref(conn, "pagerduty", "PD-?") is None


# ── 3. nobody says anything ───────────────────────────────────────────────


def test_silence_threshold_scales_with_each_incidents_own_rhythm():
    kwargs = dict(
        multiplier=6.0,
        critical_multiplier=20.0,
        floor_ms=0,
        ceiling_ms=10_000_000,
    )

    chatty = silence_threshold_ms(1_000.0, 0.0, "high", **kwargs)
    quiet = silence_threshold_ms(60_000.0, 0.0, "high", **kwargs)
    assert quiet > chatty, "an hourly alert is not overdue as fast as a chatty one"

    # Criticals are held to a far longer window: wrongly closing a payment
    # outage is much worse than leaving it open longer.
    critical = silence_threshold_ms(1_000.0, 0.0, "critical", **kwargs)
    assert critical > chatty

    # Uncertainty widens the window rather than narrowing it.
    assert silence_threshold_ms(1_000.0, 250_000.0, "high", **kwargs) > chatty


def test_silence_threshold_is_clamped_at_both_ends():
    kwargs = dict(multiplier=6.0, critical_multiplier=20.0,
                  floor_ms=900_000, ceiling_ms=3_600_000)

    # A brand-new incident with no history must not resolve seconds after it
    # opens, and a slow-cycling one must not stay open for days.
    assert silence_threshold_ms(0.0, 0.0, "high", **kwargs) == 900_000
    assert silence_threshold_ms(10_000_000.0, 0.0, "high", **kwargs) == 3_600_000


@pytest.mark.asyncio
async def test_a_quiet_incident_is_presumed_resolved_and_labelled_as_such(db):
    conn = db.writer_conn
    long_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    incident = await _seed(conn, "inc-quiet", last_alert_at=long_ago, mean_gap=5_000.0)

    resolved = await SilenceSweeper(db).sweep_once()
    assert resolved == 1

    row = await _status(conn, incident)
    assert row["status"] == "RESOLVED"
    assert row["resolution_source"] == INFERRED_SILENCE
    assert row["resolved_via"] == "system"
    # The claim must be legible as an inference, not as a confirmation.
    assert "Presumed resolved" in row["resolution_detail"]


@pytest.mark.asyncio
async def test_a_still_firing_incident_is_left_alone(db):
    conn = db.writer_conn
    incident = await _seed(conn, "inc-live", last_alert_at=datetime.now(timezone.utc))

    assert await SilenceSweeper(db).sweep_once() == 0
    assert (await _status(conn, incident))["status"] == "ACKNOWLEDGED"


@pytest.mark.asyncio
async def test_a_quiet_critical_is_held_to_a_longer_window(db):
    conn = db.writer_conn
    quiet_since = datetime.now(timezone.utc) - timedelta(minutes=20)
    critical = await _seed(conn, "inc-crit", severity="critical", last_alert_at=quiet_since)
    high = await _seed(conn, "inc-high", severity="high", last_alert_at=quiet_since)

    await SilenceSweeper(db).sweep_once()

    assert (await _status(conn, high))["status"] == "RESOLVED"
    assert (await _status(conn, critical))["status"] == "ACKNOWLEDGED", (
        "a critical must not be closed on 20 minutes of quiet"
    )


@pytest.mark.asyncio
async def test_the_sweep_notifies_both_channels(db):
    conn = db.writer_conn
    incident = await _seed(
        conn, "inc-notify", last_alert_at=datetime.now(timezone.utc) - timedelta(hours=3)
    )

    await SilenceSweeper(db).sweep_once()

    async with conn.execute(
        "SELECT channel, action FROM outbox WHERE incident_id = ?", (incident,)
    ) as cursor:
        rows = [dict(row) for row in await cursor.fetchall()]

    assert {row["channel"] for row in rows} == {"slack", "pagerduty"}
    assert all(row["action"] == "resolve" for row in rows)


# ── the outage case that started all this ─────────────────────────────────


@pytest.mark.asyncio
async def test_an_incident_handled_during_an_outage_does_not_come_back_actionable(db):
    """The question this whole plane exists to answer.

    Slack is down, the critical fails over to PagerDuty, an engineer
    acknowledges and fixes it there. When Slack returns, the card must reflect
    that - not post a fresh Acknowledge button for finished work.
    """

    conn = db.writer_conn
    incident = await _seed(conn, "inc-handled", status="OPEN", severity="critical")

    await reconcile.apply_external_action(
        conn,
        ExternalAction(
            inbound_id="pagerduty:evt-ack",
            provider="pagerduty",
            kind=ACKNOWLEDGE,
            incident_id=incident,
            actor="dana",
        ),
    )
    await reconcile.apply_external_action(
        conn,
        ExternalAction(
            inbound_id="pagerduty:evt-res",
            provider="pagerduty",
            kind=RESOLVE,
            incident_id=incident,
            actor="dana",
        ),
    )
    await conn.commit()

    # Slack comes back and the queued card is rendered from current state.
    payload = await recovery.hydrate_payload(
        conn, incident, {"incident_id": incident}
    )

    assert payload["state"] == "RESOLVED"
    assert payload["acknowledged_by"] == "dana"
    assert payload["via"] == "pagerduty"
    assert recovery.effective_action("create", payload["state"]) == "resolve"

    from src.outbox.slack import _build_blocks

    rendered = json.dumps(_build_blocks(payload, resolved=True))
    assert "Resolved" in rendered
    assert "dana" in rendered
    assert "acknowledge_incident" not in rendered, "no button for finished work"
