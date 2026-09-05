"""What happens to the backlog when a channel comes back.

Draining a queue naively after an outage is its own incident. A 30-minute
Slack outage over a noisy service leaves hundreds of rows, most of which are
successive ``update`` intents for a handful of incidents whose state has moved
on since. Replaying them in order would post a burst of stale cards - exactly
the alert fatigue this system exists to remove, caused by the system itself.

Three rules avoid that:

*Coalesce.* For each ``(incident_id, channel)`` only the newest pending intent
survives; the rest are marked ``superseded`` and never sent. One incident that
changed forty times during the outage produces one message.

*Render late.* A queued row records the *intent* to notify, not the message
text. The body is rendered from current incident state at send time, so a card
delivered after a long outage describes the incident as it is now, including
whether it already resolved while nobody could see it.

*Explain the gap.* One digest is posted ahead of the backlog stating exactly
what the blind window was and what happened inside it, so the channel history
is not silently missing half an hour.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import aiosqlite

from src.graph.storm_grouping import group_for_anchor, group_snapshot


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


async def coalesce_pending(conn: aiosqlite.Connection, channel: str) -> int:
    """Collapse each incident's queued intents down to its newest one.

    Returns the number of rows superseded. Rows already failed over to another
    channel are left alone: they represent a delivery that has been handed to a
    different provider, not a duplicate of this one.
    """

    async with conn.execute(
        """
        SELECT incident_id, MAX(outbox_id) AS winner, COUNT(*) AS queued
        FROM outbox
        WHERE channel = ? AND status = 'pending'
        GROUP BY incident_id
        HAVING COUNT(*) > 1
        """,
        (channel,),
    ) as cursor:
        groups = [dict(row) for row in await cursor.fetchall()]

    superseded = 0
    for group in groups:
        # Carry forward the marker saying this incident was already paged out
        # through a fallback. It may live on a row that is about to be
        # superseded, and losing it would make the recovered card read as a
        # new, unhandled incident next to the PagerDuty alert for the same one.
        async with conn.execute(
            """
            SELECT json_extract(payload_json, '$.delivered_via_fallback') AS via
            FROM outbox
            WHERE channel = ? AND status = 'pending' AND incident_id = ?
              AND json_extract(payload_json, '$.delivered_via_fallback') IS NOT NULL
            ORDER BY outbox_id DESC LIMIT 1
            """,
            (channel, group["incident_id"]),
        ) as marker_cursor:
            marker = await marker_cursor.fetchone()

        if marker is not None:
            await conn.execute(
                """
                UPDATE outbox
                SET payload_json = json_set(payload_json, '$.delivered_via_fallback', ?)
                WHERE outbox_id = ?
                """,
                (marker["via"], group["winner"]),
            )

        cursor = await conn.execute(
            """
            UPDATE outbox
            SET status = 'superseded', superseded_by = ?
            WHERE channel = ? AND status = 'pending'
              AND incident_id = ? AND outbox_id < ?
            """,
            (group["winner"], channel, group["incident_id"], group["winner"]),
        )
        superseded += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    return superseded


async def hydrate_payload(
    conn: aiosqlite.Connection, incident_id: str, payload: dict
) -> dict:
    """Fill a queued payload from the incident's current state.

    The engine writes a small intent payload (ids, state, counts). Everything a
    human reads - title, severity, how many alerts it ended up collapsing, the
    graph's root-cause hint - is read here, at delivery time, so it is never
    stale and never the placeholder text a partial payload would render.
    """

    async with conn.execute(
        """
        SELECT title, summary, severity, status, alert_count,
               root_cause_hint, first_alert_at, last_alert_at, updated_at,
               acknowledged_by, acknowledged_via, resolved_via, resolution_source,
               COALESCE(reopen_count, 0) AS reopen_count, flapping_since
        FROM incidents WHERE incident_id = ?
        """,
        (incident_id,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return payload

    hydrated = dict(payload)
    hydrated.setdefault("incident_id", incident_id)
    hydrated["title"] = row["title"]
    hydrated["summary"] = row["summary"] or hydrated.get("summary") or ""
    hydrated["severity"] = row["severity"]
    hydrated["state"] = row["status"]
    hydrated["alert_count"] = int(row["alert_count"])
    hydrated["root_cause_hint"] = row["root_cause_hint"]
    hydrated["first_alert_at"] = row["first_alert_at"]
    hydrated["timestamp"] = row["last_alert_at"]

    # Whoever handled this incident while the channel was down handled it for
    # real. Carrying the provenance through means the recovered card shows an
    # acknowledgement instead of an Acknowledge button that invites a second
    # responder to start work somebody already finished.
    if row["acknowledged_by"]:
        hydrated["acknowledged_by"] = row["acknowledged_by"]
    if row["acknowledged_via"] or row["resolved_via"]:
        hydrated["via"] = row["resolved_via"] or row["acknowledged_via"]
    if row["resolution_source"]:
        hydrated["resolution_source"] = row["resolution_source"]

    # A repeatedly reopened incident is a finding about the alert rule, not
    # just about the service. Carried onto the card because this system is the
    # only thing in the stack positioned to notice it.
    if row["flapping_since"]:
        hydrated["flapping"] = True
        hydrated["reopen_count"] = int(row["reopen_count"])

    # If this incident anchors a correlated storm, the card is the storm's, not
    # its own. Attaching the snapshot here means the group is rendered from
    # live membership at send time, so a cascade that grew during an outage
    # arrives complete rather than as it looked when the first alert landed.
    group_id = await group_for_anchor(conn, incident_id)
    if group_id is not None:
        snapshot = await group_snapshot(conn, group_id)
        if snapshot is not None and snapshot["member_count"] > 1:
            hydrated["group"] = snapshot
            hydrated["title"] = snapshot["title"]
            hydrated["severity"] = snapshot["severity"]
            hydrated["alert_count"] = snapshot["total_alert_count"]

    return hydrated


def effective_action(queued_action: str, incident_status: str | None) -> str:
    """Reconcile a queued action with where the incident actually ended up.

    An incident that opened and resolved entirely inside the outage window has
    a queued ``create`` and a live status of ``RESOLVED``. Posting the create
    unchanged would announce an active incident that is already over.
    """

    if incident_status == "RESOLVED":
        return "resolve"
    return queued_action


async def build_recovery_digest(
    conn: aiosqlite.Connection,
    channel: str,
    outage_start: datetime | None,
    recovered_at: datetime,
) -> dict | None:
    """Summarise what the channel missed. Returns a payload, or None if nothing."""

    if outage_start is None:
        return None

    window_start = _iso(outage_start)
    window_end = _iso(recovered_at)

    async with conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END) AS critical,
            SUM(CASE WHEN status = 'RESOLVED' THEN 1 ELSE 0 END) AS resolved
        FROM incidents
        WHERE updated_at >= ? AND updated_at <= ?
        """,
        (window_start, window_end),
    ) as cursor:
        incident_row = await cursor.fetchone()

    async with conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status = 'superseded' THEN 1 ELSE 0 END) AS superseded,
            SUM(CASE WHEN failover_of IS NOT NULL THEN 1 ELSE 0 END) AS failed_over
        FROM outbox
        WHERE created_at >= ? AND created_at <= ?
        """,
        (window_start, window_end),
    ) as cursor:
        queue_row = await cursor.fetchone()

    total = int(incident_row["total"] or 0)
    if total == 0 and int(queue_row["pending"] or 0) == 0:
        return None

    duration_seconds = max(0, int((recovered_at - outage_start).total_seconds()))

    return {
        "kind": "recovery_digest",
        "channel": channel,
        "outage_started_at": window_start,
        "recovered_at": window_end,
        "duration_seconds": duration_seconds,
        "incidents_touched": total,
        "critical_incidents": int(incident_row["critical"] or 0),
        "resolved_during_outage": int(incident_row["resolved"] or 0),
        "queued_messages": int(queue_row["pending"] or 0),
        "collapsed_messages": int(queue_row["superseded"] or 0),
        "delivered_via_fallback": int(queue_row["failed_over"] or 0),
    }


async def enqueue_recovery_digest(
    conn: aiosqlite.Connection, channel: str, digest: dict
) -> None:
    """Queue the digest ahead of the backlog it explains.

    Priority -1 puts it before every severity class, so the channel reads as
    "here is the gap, and here is what happened in it" rather than an
    unexplained burst of cards.
    """

    now = _iso(datetime.now(timezone.utc))
    await conn.execute(
        """
        INSERT INTO outbox (
            incident_id, channel, action, payload_json, status,
            next_attempt_at, priority, origin_channel
        ) VALUES (?, ?, 'create', ?, 'pending', ?, -1, ?)
        """,
        (
            f"digest:{channel}:{digest['outage_started_at']}",
            channel,
            json.dumps(digest, sort_keys=True, separators=(",", ":")),
            now,
            channel,
        ),
    )


__all__ = [
    "build_recovery_digest",
    "coalesce_pending",
    "effective_action",
    "enqueue_recovery_digest",
    "hydrate_payload",
]
