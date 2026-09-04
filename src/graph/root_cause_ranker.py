"""Root-cause ranking from directed, decayed-joint co-occurrence evidence.

Option B graph contract: ``edges.weight`` is the one persisted
``decayed_joint_weight``. Source and target counts are intentionally not stored
as separate columns, avoiding a risky schema migration during the hackathon.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import aiosqlite

from .edge_decay import decay_weights
from .observe_incident import DEFAULT_HALF_LIFE_MS

_ACTIVE_STATES = ("OPEN", "ACKNOWLEDGED", "QUIESCENT")


def _parse_time_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


async def rank_root_cause(
    tx: aiosqlite.Connection, *, half_life_ms: float = DEFAULT_HALF_LIFE_MS
) -> str | None:
    """Return the active node with the strongest outbound decayed_joint_weight."""

    active_placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
    async with tx.execute(
        f"""
        SELECT e.src_incident_id, e.weight, e.last_seen_at
        FROM edges AS e
        JOIN incidents AS source ON source.incident_id = e.src_incident_id
        JOIN incidents AS target ON target.incident_id = e.dst_incident_id
        WHERE source.status IN ({active_placeholders})
          AND target.status IN ({active_placeholders})
        """,
        (*_ACTIVE_STATES, *_ACTIVE_STATES),
    ) as cursor:
        rows = await cursor.fetchall()

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
