"""Collapse a correlated cascade into one storm, incrementally.

The co-occurrence graph already knew that a database failure, the API errors it
caused, and the pod restarts underneath were one event. It just could not say
so: each incident got a ``root_cause_hint`` and each still posted its own card,
leaving the responder to work out that three pages were one outage.

This module turns strong edges into membership. The result is that a cascade
notifies **once**, with the causal chain on a single card, and members that are
consequences stop competing for attention with their own cause.

Three properties the implementation is built around:

*Incremental, never a global recompute.* Grouping runs inside the same
transaction as the alert that triggered it. Recomputing connected components
over the whole graph per alert would be quadratic in active incidents - the
cost problem the graph already has - so this walks only the current incident's
strong neighbours and unions into whatever groups they already belong to.

*Bounded fan-out.* Only edges above ``GROUP_EDGE_THRESHOLD``, only against
incidents still active, and only the strongest ``MAX_NEIGHBOURS``. A storm that
makes everything co-occur with everything must not make grouping unbounded.

*Anchor is stable, root is not.* The anchor owns the Slack message for the
group's whole life; the ranked root cause is free to change as evidence
accumulates. Conflating them would move the card to a new message mid-incident.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from uuid import uuid4

import aiosqlite

# An edge must carry real repeated evidence before it merges two incidents into
# one story. A single coincidental co-occurrence creates an edge at weight 1.0,
# so the threshold sits above that: grouping should need corroboration.
GROUP_EDGE_THRESHOLD = 1.5

# Cap on how many strong neighbours one alert will consider. During a broad
# event every incident correlates with every other, and without this the work
# per alert would grow with the size of the storm.
MAX_NEIGHBOURS = 25

_ACTIVE_STATES = ("OPEN", "ACKNOWLEDGED", "QUIESCENT")
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(frozen=True)
class GroupAssignment:
    group_id: str
    anchor_incident_id: str
    member_count: int
    created: bool

    @property
    def is_multi_member(self) -> bool:
        return self.member_count > 1


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now() -> str:
    return _iso(datetime.now(timezone.utc))


async def _strong_neighbours(
    tx: aiosqlite.Connection, incident_id: str, scope_key: str
) -> list[str]:
    """Active incidents tied to this one by an edge worth grouping on.

    Edges are directed, so both directions are considered: being caused by
    something and causing something are equally good reasons to be part of the
    same story.
    """

    placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
    async with tx.execute(
        f"""
        SELECT other_id, MAX(weight) AS weight FROM (
            SELECT e.dst_incident_id AS other_id, e.weight AS weight
            FROM edges AS e
            WHERE e.src_incident_id = ? AND e.weight >= ?
            UNION ALL
            SELECT e.src_incident_id AS other_id, e.weight AS weight
            FROM edges AS e
            WHERE e.dst_incident_id = ? AND e.weight >= ?
        )
        WHERE other_id IN (
            SELECT incident_id FROM incidents
            WHERE scope_key = ? AND status IN ({placeholders})
        )
        AND other_id != ?
        GROUP BY other_id
        ORDER BY weight DESC
        LIMIT ?
        """,
        (
            incident_id,
            GROUP_EDGE_THRESHOLD,
            incident_id,
            GROUP_EDGE_THRESHOLD,
            scope_key,
            *_ACTIVE_STATES,
            incident_id,
            MAX_NEIGHBOURS,
        ),
    ) as cursor:
        return [row["other_id"] for row in await cursor.fetchall()]


async def _groups_of(
    tx: aiosqlite.Connection, incident_ids: list[str]
) -> list[str]:
    if not incident_ids:
        return []
    placeholders = ", ".join("?" for _ in incident_ids)
    async with tx.execute(
        f"""
        SELECT DISTINCT correlation_group_id FROM incidents
        WHERE incident_id IN ({placeholders})
          AND correlation_group_id IS NOT NULL
        """,
        incident_ids,
    ) as cursor:
        return [row["correlation_group_id"] for row in await cursor.fetchall()]


async def _oldest_group(tx: aiosqlite.Connection, group_ids: list[str]) -> str:
    """Pick the survivor when two groups turn out to be one storm.

    The oldest wins, because its anchor already owns a posted Slack message.
    Choosing a younger group would abandon a card responders are already
    looking at.
    """

    placeholders = ", ".join("?" for _ in group_ids)
    async with tx.execute(
        f"""
        SELECT group_id FROM incident_groups
        WHERE group_id IN ({placeholders})
        ORDER BY created_at ASC, group_id ASC LIMIT 1
        """,
        group_ids,
    ) as cursor:
        row = await cursor.fetchone()
    return row["group_id"] if row else group_ids[0]


async def _merge_groups(
    tx: aiosqlite.Connection, survivor: str, absorbed: list[str]
) -> None:
    if not absorbed:
        return
    placeholders = ", ".join("?" for _ in absorbed)
    await tx.execute(
        f"""
        UPDATE incidents SET correlation_group_id = ?
        WHERE correlation_group_id IN ({placeholders})
        """,
        (survivor, *absorbed),
    )
    await tx.execute(
        f"DELETE FROM incident_groups WHERE group_id IN ({placeholders})",
        absorbed,
    )


async def _refresh_group(tx: aiosqlite.Connection, group_id: str) -> int:
    """Recompute the group's derived facts from its current membership."""

    async with tx.execute(
        """
        SELECT incident_id, title, severity, status, root_cause_hint
        FROM incidents WHERE correlation_group_id = ?
        ORDER BY first_alert_at ASC, incident_id ASC
        """,
        (group_id,),
    ) as cursor:
        members = [dict(row) for row in await cursor.fetchall()]

    if not members:
        await tx.execute("DELETE FROM incident_groups WHERE group_id = ?", (group_id,))
        return 0

    # The group is as urgent as its most urgent member: a cascade containing a
    # critical is a critical, whatever the incident that started it looked like.
    severity = min(
        (member["severity"] for member in members),
        key=lambda value: _SEVERITY_RANK.get(value, 99),
    )
    # A storm is over only when every part of it is.
    active = [m for m in members if m["status"] in _ACTIVE_STATES]
    status = "OPEN" if active else "RESOLVED"
    root_id = await _rank_group_root(tx, [m["incident_id"] for m in members])
    lead = next((m for m in members if m["incident_id"] == root_id), members[0])

    await tx.execute(
        """
        UPDATE incident_groups
        SET root_incident_id = ?, title = ?, severity = ?, member_count = ?,
            status = ?, updated_at = ?
        WHERE group_id = ?
        """,
        (
            root_id,
            f"{lead['title']} (+{len(members) - 1} correlated)",
            severity,
            len(members),
            status,
            _now(),
            group_id,
        ),
    )
    return len(members)


async def _rank_group_root(
    tx: aiosqlite.Connection, member_ids: list[str]
) -> str | None:
    """The member that most leads the others is the likely cause.

    Ranking is confined to edges *inside* the group. The global ranker answers
    "what is the loudest thing anywhere", which is a different and much less
    useful question once a storm has been isolated.
    """

    if not member_ids:
        return None
    placeholders = ", ".join("?" for _ in member_ids)
    async with tx.execute(
        f"""
        SELECT src_incident_id, SUM(weight) AS outbound
        FROM edges
        WHERE src_incident_id IN ({placeholders})
          AND dst_incident_id IN ({placeholders})
        GROUP BY src_incident_id
        ORDER BY outbound DESC, src_incident_id ASC
        LIMIT 1
        """,
        (*member_ids, *member_ids),
    ) as cursor:
        row = await cursor.fetchone()
    return row["src_incident_id"] if row else member_ids[0]


async def assign_group(
    tx: aiosqlite.Connection, incident_id: str, scope_key: str
) -> GroupAssignment | None:
    """Place one incident into a storm, creating or merging groups as needed.

    Returns ``None`` when the incident has no strong correlation and should
    keep posting its own card, which is the common case.
    """

    neighbours = await _strong_neighbours(tx, incident_id, scope_key)
    if not neighbours:
        return None

    existing = await _groups_of(tx, [incident_id, *neighbours])

    if existing:
        group_id = await _oldest_group(tx, existing)
        await _merge_groups(tx, group_id, [g for g in existing if g != group_id])
        created = False
    else:
        group_id = str(uuid4())
        # The first incident of the cascade anchors it, so the card lands where
        # the story started.
        async with tx.execute(
            f"""
            SELECT incident_id FROM incidents
            WHERE incident_id IN ({", ".join("?" for _ in [incident_id, *neighbours])})
            ORDER BY first_alert_at ASC, incident_id ASC LIMIT 1
            """,
            (incident_id, *neighbours),
        ) as cursor:
            row = await cursor.fetchone()
        anchor = row["incident_id"] if row else incident_id
        await tx.execute(
            """
            INSERT INTO incident_groups (group_id, scope_key, anchor_incident_id)
            VALUES (?, ?, ?)
            """,
            (group_id, scope_key, anchor),
        )
        created = True

    members = [incident_id, *neighbours]
    placeholders = ", ".join("?" for _ in members)
    await tx.execute(
        f"""
        UPDATE incidents SET correlation_group_id = ?
        WHERE incident_id IN ({placeholders})
          AND (correlation_group_id IS NULL OR correlation_group_id != ?)
        """,
        (group_id, *members, group_id),
    )

    member_count = await _refresh_group(tx, group_id)

    async with tx.execute(
        "SELECT anchor_incident_id FROM incident_groups WHERE group_id = ?",
        (group_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None

    return GroupAssignment(
        group_id=group_id,
        anchor_incident_id=row["anchor_incident_id"],
        member_count=member_count,
        created=created,
    )


async def refresh_group_for_member(
    tx: aiosqlite.Connection, incident_id: str
) -> GroupAssignment | None:
    """Recompute the storm this incident belongs to, after its state changed.

    Group facts - severity, member count, and above all whether the storm is
    over - are derived from membership, so they go stale the moment a member
    resolves. Formation is driven by *new alerts*, but resolution is driven by
    their absence, so without this hook a storm whose members all quietly
    closed would keep an OPEN card forever.
    """

    async with tx.execute(
        "SELECT correlation_group_id FROM incidents WHERE incident_id = ?",
        (incident_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or not row["correlation_group_id"]:
        return None

    group_id = row["correlation_group_id"]
    member_count = await _refresh_group(tx, group_id)
    if member_count == 0:
        return None

    async with tx.execute(
        "SELECT anchor_incident_id FROM incident_groups WHERE group_id = ?",
        (group_id,),
    ) as cursor:
        group = await cursor.fetchone()
    if group is None:
        return None

    return GroupAssignment(
        group_id=group_id,
        anchor_incident_id=group["anchor_incident_id"],
        member_count=member_count,
        created=False,
    )


async def redirect_member_deliveries(
    tx: aiosqlite.Connection, member_incident_id: str, anchor_incident_id: str
) -> int:
    """Point a member's queued notifications at the storm card instead.

    Once an incident is known to be part of a cascade, its own card would be a
    second page for one event. Its pending intents are retargeted at the
    anchor, where the outbox's existing per-incident coalescing collapses every
    member's update into a single edit of the storm message.

    A member that already posted a card before it was correlated gets one final
    edit pointing at the storm, so no orphaned card is left in the channel
    claiming to be a separate problem.
    """

    async with tx.execute(
        """
        SELECT COUNT(*) AS n FROM outbox
        WHERE incident_id = ? AND channel = 'slack' AND status = 'sent'
          AND external_ref IS NOT NULL
        """,
        (member_incident_id,),
    ) as cursor:
        already_posted = int((await cursor.fetchone())["n"]) > 0

    cursor = await tx.execute(
        """
        UPDATE outbox
        SET incident_id = ?, action = 'update'
        WHERE incident_id = ? AND status = 'pending'
        """,
        (anchor_incident_id, member_incident_id),
    )
    redirected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    if already_posted:
        await tx.execute(
            """
            INSERT INTO outbox (
                incident_id, channel, action, payload_json, status,
                next_attempt_at, priority, origin_channel
            ) VALUES (?, 'slack', 'update', ?, 'pending', ?, 2, 'slack')
            """,
            (
                member_incident_id,
                json.dumps(
                    {
                        "incident_id": member_incident_id,
                        "merged_into": anchor_incident_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                _now(),
            ),
        )

    return redirected


async def group_snapshot(tx: aiosqlite.Connection, group_id: str) -> dict | None:
    """Everything needed to render one storm card."""

    async with tx.execute(
        """
        SELECT group_id, scope_key, anchor_incident_id, root_incident_id,
               title, severity, member_count, status, created_at, updated_at
        FROM incident_groups WHERE group_id = ?
        """,
        (group_id,),
    ) as cursor:
        group = await cursor.fetchone()
    if group is None:
        return None

    async with tx.execute(
        """
        SELECT incident_id, title, service_hint AS service, severity, status,
               alert_count, first_alert_at
        FROM (
            SELECT i.incident_id, i.title, i.severity, i.status, i.alert_count,
                   i.first_alert_at,
                   (SELECT r.service FROM raw_events AS r
                    WHERE r.incident_id = i.incident_id
                    ORDER BY r.seq ASC LIMIT 1) AS service_hint
            FROM incidents AS i
            WHERE i.correlation_group_id = ?
        )
        ORDER BY first_alert_at ASC, incident_id ASC
        """,
        (group_id,),
    ) as cursor:
        members = [dict(row) for row in await cursor.fetchall()]

    total_alerts = sum(int(member["alert_count"]) for member in members)
    return {
        "group_id": group["group_id"],
        "anchor_incident_id": group["anchor_incident_id"],
        "root_incident_id": group["root_incident_id"],
        "title": group["title"],
        "severity": group["severity"],
        "status": group["status"],
        "member_count": group["member_count"],
        "total_alert_count": total_alerts,
        "members": members,
        "started_at": group["created_at"],
    }


async def group_for_anchor(
    tx: aiosqlite.Connection, incident_id: str
) -> str | None:
    async with tx.execute(
        "SELECT group_id FROM incident_groups WHERE anchor_incident_id = ?",
        (incident_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row["group_id"] if row else None


__all__ = [
    "GROUP_EDGE_THRESHOLD",
    "MAX_NEIGHBOURS",
    "GroupAssignment",
    "assign_group",
    "group_for_anchor",
    "redirect_member_deliveries",
    "refresh_group_for_member",
    "group_snapshot",
]
