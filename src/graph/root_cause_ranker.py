"""Root-cause ranking from directed, decayed-joint co-occurrence evidence.

Option B graph contract: ``edges.weight`` is the one persisted
``decayed_joint_weight``. Source and target counts are intentionally not stored
as separate columns, avoiding a risky schema migration during the hackathon.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone

import aiosqlite

from .edge_decay import decay_weights
from .observe_incident import DEFAULT_HALF_LIFE_MS

_ACTIVE_STATES = ("OPEN", "ACKNOWLEDGED", "QUIESCENT")

# Backstop for the unrestricted path: rank on the most recent evidence rather
# than letting one alert scan an unbounded edge table.
DEFAULT_MAX_EDGES = 500


def _parse_time_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


async def rank_root_cause(
    tx: aiosqlite.Connection,
    *,
    half_life_ms: float = DEFAULT_HALF_LIFE_MS,
    candidate_ids: Sequence[str] | None = None,
    max_edges: int | None = None,
) -> str | None:
    """Return the node with the strongest outbound decayed_joint_weight.

    ``candidate_ids`` restricts ranking to one bounded neighbourhood, which is
    how the write path calls it. That matters twice over. It caps the work: the
    unrestricted query scans every edge between active incidents, which grows
    with the square of the active set and ran on every single alert. And it
    sharpens the answer: ranked globally, a large unrelated incident elsewhere
    in the scope can outweigh the actual leader of the cascade being explained,
    so the hint attached to a card described a different event entirely.

    ``max_edges`` is a backstop for the unrestricted path, so a pathological
    graph degrades the hint rather than the write transaction.
    """

    if candidate_ids is not None:
        unique_ids = tuple(dict.fromkeys(candidate_ids))
        # Fewer than two nodes cannot express "this led to that".
        if len(unique_ids) < 2:
            return None
        id_placeholders = ", ".join("?" for _ in unique_ids)
        async with tx.execute(
            f"""
            SELECT src_incident_id, weight, last_seen_at
            FROM edges
            WHERE src_incident_id IN ({id_placeholders})
              AND dst_incident_id IN ({id_placeholders})
            """,
            (*unique_ids, *unique_ids),
        ) as cursor:
            rows = await cursor.fetchall()
        return _rank(rows, half_life_ms)

    active_placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
    limit = max_edges if max_edges is not None else DEFAULT_MAX_EDGES
    async with tx.execute(
        f"""
        SELECT e.src_incident_id, e.weight, e.last_seen_at
        FROM edges AS e
        JOIN incidents AS source ON source.incident_id = e.src_incident_id
        JOIN incidents AS target ON target.incident_id = e.dst_incident_id
        WHERE source.status IN ({active_placeholders})
          AND target.status IN ({active_placeholders})
        ORDER BY e.last_seen_at DESC
        LIMIT ?
        """,
        (*_ACTIVE_STATES, *_ACTIVE_STATES, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return _rank(rows, half_life_ms)


def _rank(rows, half_life_ms: float) -> str | None:
    """Score outbound decayed weight per node and name the strongest."""

    if not rows:
        return None

    reference_ms = max(_parse_time_ms(row["last_seen_at"]) for row in rows)
    scores: defaultdict[str, float] = defaultdict(float)
    for row in rows:
        elapsed_ms = max(0, reference_ms - _parse_time_ms(row["last_seen_at"]))
        score = decay_weights(
            float(row["weight"]),
            0.0,
            0.0,
            elapsed_ms,
            half_life_ms,
        ).joint
        scores[row["src_incident_id"]] += score

    root_id, score = max(scores.items(), key=lambda item: (item[1], item[0]))
    return f"root_cause={root_id}; outbound_decayed_joint_weight={score:.6f}"
