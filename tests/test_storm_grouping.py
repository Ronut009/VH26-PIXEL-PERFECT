"""One cascade should page once, not once per affected service."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from src.db.connection import Database
from src.graph.storm_grouping import (
    GROUP_EDGE_THRESHOLD,
    assign_group,
    group_for_anchor,
    group_snapshot,
    redirect_member_deliveries,
    refresh_group_for_member,
)
from src.outbox import recovery

SCOPE = "prod/eu-west"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, label))


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "storm.db"))
    await database.connect()
    yield database
    await database.close()


async def _incident(
    conn: aiosqlite.Connection,
    label: str,
    *,
    severity: str = "medium",
    status: str = "ACKNOWLEDGED",
    minutes_ago: int = 0,
    alert_count: int = 1,
) -> str:
    incident_id = _id(label)
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    await conn.execute(
        """
        INSERT INTO incidents (
            incident_id, scope_key, stable_fingerprint, title, summary, severity,
            status, alert_count, first_alert_at, last_alert_at
        ) VALUES (?, ?, ?, ?, 'summary', ?, ?, ?, ?, ?)
        """,
        (
            incident_id,
            SCOPE,
            f"fp-{label}",
            label,
            severity,
            status,
            alert_count,
            _iso(moment),
            _iso(moment),
        ),
    )
    await conn.commit()
    return incident_id


async def _edge(
    conn: aiosqlite.Connection, src: str, dst: str, weight: float
) -> None:
    await conn.execute(
        """
        INSERT INTO edges (src_incident_id, dst_incident_id, weight, last_seen_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(src_incident_id, dst_incident_id)
        DO UPDATE SET weight = excluded.weight
        """,
        (src, dst, weight, _iso(datetime.now(timezone.utc))),
    )
    await conn.commit()


async def _group_of(conn: aiosqlite.Connection, incident_id: str) -> str | None:
    async with conn.execute(
        "SELECT correlation_group_id FROM incidents WHERE incident_id = ?",
        (incident_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row["correlation_group_id"] if row else None


# ── forming a storm ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_weak_edge_does_not_merge_two_incidents(db):
    """One coincidence is not a cascade.

    A single co-occurrence creates an edge at weight 1.0. Grouping on that
    would fuse unrelated incidents that merely broke at the same moment, which
    is worse than not grouping at all.
    """

    conn = db.writer_conn
    database = await _incident(conn, "db-primary", minutes_ago=5)
    api = await _incident(conn, "api-gateway", minutes_ago=4)
    await _edge(conn, database, api, GROUP_EDGE_THRESHOLD - 0.4)

    assert await assign_group(conn, api, SCOPE) is None
    assert await _group_of(conn, api) is None


@pytest.mark.asyncio
async def test_a_cascade_becomes_one_group(db):
    conn = db.writer_conn
    database = await _incident(conn, "db-primary", severity="high", minutes_ago=5)
    api = await _incident(conn, "api-gateway", minutes_ago=4)
    pods = await _incident(conn, "pod-restarts", minutes_ago=3)

    await _edge(conn, database, api, 3.0)
    await _edge(conn, api, pods, 2.4)

    assert await assign_group(conn, api, SCOPE) is not None
    assignment = await assign_group(conn, pods, SCOPE)
    await conn.commit()

    assert assignment is not None
    assert assignment.member_count == 3
    # The earliest incident anchors the card, so the story starts where it began.
    assert assignment.anchor_incident_id == database

    groups = {await _group_of(conn, i) for i in (database, api, pods)}
    assert len(groups) == 1 and None not in groups


@pytest.mark.asyncio
async def test_two_separate_storms_merge_when_evidence_connects_them(db):
    """Cascades are discovered incrementally, so two halves can form apart."""

    conn = db.writer_conn
    a1 = await _incident(conn, "storm-a-1", minutes_ago=10)
    a2 = await _incident(conn, "storm-a-2", minutes_ago=9)
    b1 = await _incident(conn, "storm-b-1", minutes_ago=5)
    b2 = await _incident(conn, "storm-b-2", minutes_ago=4)

    await _edge(conn, a1, a2, 3.0)
    await _edge(conn, b1, b2, 3.0)
    await assign_group(conn, a2, SCOPE)
    await assign_group(conn, b2, SCOPE)
    await conn.commit()

    first, second = await _group_of(conn, a1), await _group_of(conn, b1)
    assert first != second, "they start as separate storms"

    # Later evidence ties the two halves together.
    await _edge(conn, a2, b1, 3.0)
    assignment = await assign_group(conn, b1, SCOPE)
    await conn.commit()

    assert assignment is not None
    assert assignment.member_count == 4
    assert len({await _group_of(conn, i) for i in (a1, a2, b1, b2)}) == 1
    # The older group survives, because its anchor already owns a posted card.
    assert assignment.group_id == first
    assert assignment.anchor_incident_id == a1


@pytest.mark.asyncio
async def test_the_group_takes_its_most_urgent_members_severity(db):
    conn = db.writer_conn
    low = await _incident(conn, "noisy-low", severity="low", minutes_ago=5)
    crit = await _incident(conn, "payment-critical", severity="critical", minutes_ago=4)
    await _edge(conn, low, crit, 3.0)

    assignment = await assign_group(conn, crit, SCOPE)
    await conn.commit()

    snapshot = await group_snapshot(conn, assignment.group_id)
    assert snapshot["severity"] == "critical", (
        "a cascade containing a critical is a critical"
    )


@pytest.mark.asyncio
async def test_the_root_is_ranked_inside_the_group_not_globally(db):
    conn = db.writer_conn
    database = await _incident(conn, "db-primary", minutes_ago=5)
    api = await _incident(conn, "api-gateway", minutes_ago=4)
    pods = await _incident(conn, "pod-restarts", minutes_ago=3)
    # An unrelated loud incident that would dominate a global ranking.
    noisy = await _incident(conn, "unrelated-noisy", minutes_ago=6)
    other = await _incident(conn, "unrelated-other", minutes_ago=6)
    await _edge(conn, noisy, other, 50.0)

    await _edge(conn, database, api, 3.0)
    await _edge(conn, database, pods, 3.0)
    await _edge(conn, api, pods, 2.0)

    await assign_group(conn, api, SCOPE)
    assignment = await assign_group(conn, pods, SCOPE)
    await conn.commit()

    snapshot = await group_snapshot(conn, assignment.group_id)
    assert snapshot["root_incident_id"] == database
    assert noisy not in [m["incident_id"] for m in snapshot["members"]]


@pytest.mark.asyncio
async def test_a_storm_closes_only_when_every_member_is_closed(db):
    conn = db.writer_conn
    database = await _incident(conn, "db-primary", minutes_ago=5)
    api = await _incident(conn, "api-gateway", minutes_ago=4)
    await _edge(conn, database, api, 3.0)
    assignment = await assign_group(conn, api, SCOPE)
    await conn.commit()

    await conn.execute(
        "UPDATE incidents SET status = 'RESOLVED' WHERE incident_id = ?", (api,)
    )
    await refresh_group_for_member(conn, api)
    await conn.commit()
    assert (await group_snapshot(conn, assignment.group_id))["status"] == "OPEN"

    await conn.execute(
        "UPDATE incidents SET status = 'RESOLVED' WHERE incident_id = ?", (database,)
    )
    await refresh_group_for_member(conn, database)
    await conn.commit()
    assert (await group_snapshot(conn, assignment.group_id))["status"] == "RESOLVED"


# ── delivering a storm ────────────────────────────────────────────────────


async def _queue(conn: aiosqlite.Connection, incident_id: str, action: str) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO outbox (incident_id, channel, action, payload_json, status,
                            next_attempt_at, priority, origin_channel)
        VALUES (?, 'slack', ?, ?, 'pending', ?, 2, 'slack')
        """,
        (
            incident_id,
            action,
            json.dumps({"incident_id": incident_id}),
            _iso(datetime.now(timezone.utc)),
        ),
    )
    await conn.commit()
    return cursor.lastrowid


@pytest.mark.asyncio
async def test_member_notifications_are_redirected_to_the_storm_card(db):
    conn = db.writer_conn
    database = await _incident(conn, "db-primary", minutes_ago=5)
    api = await _incident(conn, "api-gateway", minutes_ago=4)
    await _edge(conn, database, api, 3.0)

    await _queue(conn, api, "create")
    await redirect_member_deliveries(conn, api, database)
    await conn.commit()

    async with conn.execute(
        "SELECT incident_id, action FROM outbox WHERE status = 'pending'"
    ) as cursor:
        rows = [dict(row) for row in await cursor.fetchall()]

    assert len(rows) == 1
    assert rows[0]["incident_id"] == database, "the member's card became the storm's"
    assert rows[0]["action"] == "update"


@pytest.mark.asyncio
async def test_an_already_posted_member_card_is_not_left_orphaned(db):
    """A card posted before correlation must not keep claiming to be separate."""

    conn = db.writer_conn
    database = await _incident(conn, "db-primary", minutes_ago=5)
    api = await _incident(conn, "api-gateway", minutes_ago=4)

    sent_id = await _queue(conn, api, "create")
    await conn.execute(
        "UPDATE outbox SET status = 'sent', external_ref = '111.222' WHERE outbox_id = ?",
        (sent_id,),
    )
    await conn.commit()

    await redirect_member_deliveries(conn, api, database)
    await conn.commit()

    async with conn.execute(
        "SELECT incident_id, payload_json FROM outbox WHERE status = 'pending'"
    ) as cursor:
        rows = [dict(row) for row in await cursor.fetchall()]

    merged = [r for r in rows if json.loads(r["payload_json"]).get("merged_into")]
    assert len(merged) == 1
    assert merged[0]["incident_id"] == api
    assert json.loads(merged[0]["payload_json"])["merged_into"] == database


@pytest.mark.asyncio
async def test_the_anchor_card_renders_the_whole_storm(db):
    conn = db.writer_conn
    database = await _incident(
        conn, "db-primary", severity="high", minutes_ago=5, alert_count=12
    )
    api = await _incident(conn, "api-gateway", minutes_ago=4, alert_count=300)
    pods = await _incident(conn, "pod-restarts", minutes_ago=3, alert_count=205)
    await _edge(conn, database, api, 3.0)
    await _edge(conn, database, pods, 3.0)
    await assign_group(conn, api, SCOPE)
    await assign_group(conn, pods, SCOPE)
    await conn.commit()

    assert await group_for_anchor(conn, database) is not None

    payload = await recovery.hydrate_payload(conn, database, {"incident_id": database})
    assert payload["group"]["member_count"] == 3
    # The headline count is the whole storm, not just the anchor's own alerts.
    assert payload["alert_count"] == 12 + 300 + 205

    from src.outbox.slack import _build_blocks

    rendered = json.dumps(_build_blocks(payload))
    assert "Correlated storm" in rendered
    assert "Likely cause" in rendered
    assert "Downstream effects" in rendered
    assert "one page instead of 3" in rendered


@pytest.mark.asyncio
async def test_an_ungrouped_incident_still_renders_its_own_card(db):
    conn = db.writer_conn
    lone = await _incident(conn, "lonely-incident")

    payload = await recovery.hydrate_payload(conn, lone, {"incident_id": lone})
    assert "group" not in payload

    from src.outbox.slack import _build_blocks

    rendered = json.dumps(_build_blocks(payload))
    assert "Correlated storm" not in rendered
    assert "acknowledge_incident" in rendered
