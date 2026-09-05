"""Root-cause ranking from directed, decayed co-occurrence evidence.

The original ranker summed each node's outbound decayed weight and named the
largest. That has two failures, and they compound.

**It cannot tell a cause from something that is always on.** A chronically
unhealthy service co-occurs with everything, so it accumulates enormous
outbound weight - while accumulating just as much *inbound* weight, because it
follows as often as it leads. Summing outbound alone, it wins. The fix is to
weigh leading against following: a cause leads much more than it follows, and a
node that does both equally is a bystander, however loud.

**It always answered.** Given any edges at all it named somebody, with no way
to say the evidence was thin. During a broad event - a bad deploy, an AZ blip -
everything co-occurs with everything, the field is indistinguishable, and
naming one of them is picking arbitrarily and calling it analysis. A responder
told the wrong root cause once stops reading the field entirely, so a wrong
answer costs more than no answer. Confidence here comes from *separation*: how
far clear the leader is of the runner-up, scaled by how much evidence exists at
all. Below the threshold this returns ``None`` and the card says nothing.

Co-occurrence counts are also normalised by **lift** - ``P(a,b) / P(a)P(b)`` -
using the marginals recorded in ``graph_node_stats`` and ``graph_scope_stats``.
Two services that each fire constantly co-occur constantly by chance; lift
divides that chance out, so a pair that co-occurs *specifically* outranks a
pair that merely co-occurs *often*. When marginals are unavailable - an older
database, or evidence inserted directly - lift falls back to 1.0 and the model
degrades to directional weighting rather than failing.

No model is involved, deliberately. A prior plus decayed counts is something a
responder can argue with at 3am; a learned score is not.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

from .edge_decay import decay_weights
from .observe_incident import DEFAULT_HALF_LIFE_MS

_ACTIVE_STATES = ("OPEN", "ACKNOWLEDGED", "QUIESCENT")

# Backstop for the unrestricted path: rank on the most recent evidence rather
# than letting one alert scan an unbounded edge table.
DEFAULT_MAX_EDGES = 500

# One co-occurrence is a coincidence. This sits just above the 1.0 a single
# observation produces, matching the threshold storm grouping already uses to
# decide that two incidents are one event.
MIN_SUPPORT = 1.5

# Evidence at which support stops adding confidence. Six decayed co-occurrences
# is a pattern rather than a run of luck.
FULL_SUPPORT = 6.0

# Below this the leader is not clear of the field, so the honest output is
# silence rather than a coin flip stated as a conclusion.
MIN_CONFIDENCE = 0.35

# The share of a node's activity that must be *net* leading. Separation alone
# is a ratio, so it cannot tell "clearly ahead" from "two numbers that are both
# essentially zero" - when every node leads and follows in equal measure, the
# nets collapse to floating-point dust and the ratio between two specks reads
# as a landslide. This floor asks the absolute question instead: of everything
# this node did, how much of it was leading rather than following?
MIN_DOMINANCE = 0.25

# A single freak ratio - a rare pair that happened to co-occur twice - should
# tilt the ranking, not decide it.
LIFT_CAP = 4.0


@dataclass(frozen=True)
class RootCauseVerdict:
    """A ranked leader, carrying the evidence needed to argue with it."""

    incident_id: str
    confidence: float
    support: float
    mean_lift: float

    def as_hint(self) -> str:
        return (
            f"root_cause={self.incident_id}; "
            f"confidence={self.confidence:.2f}; "
            f"support={self.support:.2f}; "
            f"lift={self.mean_lift:.2f}"
        )


def _parse_time_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


async def _marginals(
    tx: aiosqlite.Connection, incident_ids: Sequence[str]
) -> tuple[dict[str, float], float]:
    """Per-node observation counts and the scope's round count.

    Returns empty marginals rather than raising when the tables are absent or
    unpopulated, so ranking degrades to directional weighting instead of
    failing on a database that predates them.
    """

    if not incident_ids:
        return {}, 0.0

    placeholders = ", ".join("?" for _ in incident_ids)
    try:
        async with tx.execute(
            f"""
            SELECT incident_id, scope_key, observations
            FROM graph_node_stats
            WHERE incident_id IN ({placeholders})
            """,
            tuple(incident_ids),
        ) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.OperationalError:
        return {}, 0.0

    if not rows:
        return {}, 0.0

    observations = {row["incident_id"]: float(row["observations"]) for row in rows}
    scope_key = rows[0]["scope_key"]

    async with tx.execute(
        "SELECT rounds FROM graph_scope_stats WHERE scope_key = ?", (scope_key,)
    ) as cursor:
        scope_row = await cursor.fetchone()

    return observations, (float(scope_row["rounds"]) if scope_row else 0.0)


def _lift(
    weight: float,
    src_id: str,
    dst_id: str,
    observations: dict[str, float],
    rounds: float,
) -> float:
    """How much more often this pair co-occurs than chance would predict.

    1.0 means exactly as often as chance. Below 1.0 the pair co-occurs *less*
    than their individual firing rates imply, which is evidence against a
    relationship rather than for one.
    """

    src_obs = observations.get(src_id, 0.0)
    dst_obs = observations.get(dst_id, 0.0)
    if rounds <= 0 or src_obs <= 0 or dst_obs <= 0:
        # No usable denominator: take the raw count at face value.
        return 1.0
    return min((weight * rounds) / (src_obs * dst_obs), LIFT_CAP)


def _score(
    rows,
    half_life_ms: float,
    observations: dict[str, float],
    rounds: float,
) -> RootCauseVerdict | None:
    """Rank by how much each node leads beyond what it follows."""

    if not rows:
        return None

    reference_ms = max(_parse_time_ms(row["last_seen_at"]) for row in rows)

    outbound: defaultdict[str, float] = defaultdict(float)
    inbound: defaultdict[str, float] = defaultdict(float)
    raw_outbound: defaultdict[str, float] = defaultdict(float)
    lift_total: defaultdict[str, float] = defaultdict(float)
    lift_count: defaultdict[str, int] = defaultdict(int)

    for row in rows:
        src_id = row["src_incident_id"]
        dst_id = row["dst_incident_id"]
        elapsed_ms = max(0, reference_ms - _parse_time_ms(row["last_seen_at"]))
        weight = decay_weights(
            float(row["weight"]), 0.0, 0.0, elapsed_ms, half_life_ms
        ).joint

        lift = _lift(weight, src_id, dst_id, observations, rounds)
        contribution = weight * lift

        outbound[src_id] += contribution
        inbound[dst_id] += contribution
        raw_outbound[src_id] += weight
        lift_total[src_id] += lift
        lift_count[src_id] += 1

    # Net lead: a cause leads far more than it follows, so a node that does
    # both in equal measure scores zero however loud it is.
    net = {
        node: outbound[node] - inbound.get(node, 0.0)
        for node in outbound
        if outbound[node] - inbound.get(node, 0.0) > 0
    }
    if not net:
        return None

    ranked = sorted(net.items(), key=lambda item: (item[1], item[0]), reverse=True)
    leader_id, leader_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

    support = raw_outbound[leader_id]
    if support < MIN_SUPPORT:
        return None

    # A node has to actually lead, not merely edge ahead of an equally
    # ambiguous field. Without this, four services that all lead and follow
    # each other - a bad deploy, exactly - leave nets near zero, and the ratio
    # between two near-zero nets reports a landslide.
    dominance = leader_score / outbound[leader_id] if outbound[leader_id] else 0.0
    if dominance < MIN_DOMINANCE:
        return None

    # Confidence is separation from the field, tempered by how much evidence
    # exists at all. A clear leader on thin evidence is still thin evidence.
    margin = (leader_score - runner_up) / leader_score
    support_factor = min(1.0, support / FULL_SUPPORT)
    confidence = margin * support_factor
    if confidence < MIN_CONFIDENCE:
        return None

    mean_lift = lift_total[leader_id] / max(1, lift_count[leader_id])
    return RootCauseVerdict(
        incident_id=leader_id,
        confidence=confidence,
        support=support,
        mean_lift=mean_lift,
    )


async def score_root_cause(
    tx: aiosqlite.Connection,
    *,
    half_life_ms: float = DEFAULT_HALF_LIFE_MS,
    candidate_ids: Sequence[str] | None = None,
    max_edges: int | None = None,
) -> RootCauseVerdict | None:
    """Rank a neighbourhood and return the verdict, or ``None`` when unclear.

    ``candidate_ids`` restricts ranking to one bounded neighbourhood, which is
    how the write path calls it. That caps the work - the unrestricted query
    scans every edge between active incidents, which grows with the square of
    the active set - and sharpens the answer, because a large unrelated
    incident elsewhere in the scope should not outweigh the actual leader of
    the cascade being explained.
    """

    if candidate_ids is not None:
        unique_ids = tuple(dict.fromkeys(candidate_ids))
        # Fewer than two nodes cannot express "this led to that".
        if len(unique_ids) < 2:
            return None
        id_placeholders = ", ".join("?" for _ in unique_ids)
        async with tx.execute(
            f"""
            SELECT src_incident_id, dst_incident_id, weight, last_seen_at
            FROM edges
            WHERE src_incident_id IN ({id_placeholders})
              AND dst_incident_id IN ({id_placeholders})
            """,
            (*unique_ids, *unique_ids),
        ) as cursor:
            rows = await cursor.fetchall()
        observations, rounds = await _marginals(tx, unique_ids)
        return _score(rows, half_life_ms, observations, rounds)

    active_placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
    limit = max_edges if max_edges is not None else DEFAULT_MAX_EDGES
    async with tx.execute(
        f"""
        SELECT e.src_incident_id, e.dst_incident_id, e.weight, e.last_seen_at
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

    node_ids = {row["src_incident_id"] for row in rows} | {
        row["dst_incident_id"] for row in rows
    }
    observations, rounds = await _marginals(tx, tuple(node_ids))
    return _score(rows, half_life_ms, observations, rounds)


async def rank_root_cause(
    tx: aiosqlite.Connection,
    *,
    half_life_ms: float = DEFAULT_HALF_LIFE_MS,
    candidate_ids: Sequence[str] | None = None,
    max_edges: int | None = None,
) -> str | None:
    """Return the root-cause hint string, or ``None`` when nothing is clear."""

    verdict = await score_root_cause(
        tx,
        half_life_ms=half_life_ms,
        candidate_ids=candidate_ids,
        max_edges=max_edges,
    )
    return verdict.as_hint() if verdict is not None else None


__all__ = [
    "DEFAULT_MAX_EDGES",
    "FULL_SUPPORT",
    "LIFT_CAP",
    "MIN_CONFIDENCE",
    "MIN_DOMINANCE",
    "MIN_SUPPORT",
    "RootCauseVerdict",
    "rank_root_cause",
    "score_root_cause",
]
