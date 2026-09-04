"""Single-transaction directed co-occurrence edge updates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

import aiosqlite

from .edge_decay import DecayedWeights, decay_weights, increment_weights

DEFAULT_HALF_LIFE_MS = 3_600_000.0
_ACTIVE_STATES = ("OPEN", "ACKNOWLEDGED", "QUIESCENT")


@dataclass(frozen=True)
class EdgeUpdate:
    source_incident_id: UUID
    target_incident_id: UUID
    joint_weight: float
    last_seen_at: str


def _parse_time_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _iso_now_from_event_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


async def _existing_edge(
    tx: aiosqlite.Connection, source_id: str, target_id: str
) -> tuple[float, str] | None:
    async with tx.execute(
        """
        SELECT weight, last_seen_at
        FROM edges
        WHERE src_incident_id = ? AND dst_incident_id = ?
        """,
        (source_id, target_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return float(row["weight"]), row["last_seen_at"]


async def _record_round(
    tx: aiosqlite.Connection,
    participants,
    *,
    scope_key: str,
    now_ms: int,
    observed_at: str,
    half_life_ms: float,
) -> None:
    """Age the marginal counters, then add this round's observations.

    Counters decay on the same half-life as the edges they normalise. If they
    did not, a service that was noisy last week would keep suppressing a real
    correlation discovered today - the denominator has to forget at the same
    rate as the numerator or the ratio drifts.
    """

    for participant in participants:
        incident_id = participant["incident_id"]
        async with tx.execute(
            "SELECT observations, last_observed_at FROM graph_node_stats"
            " WHERE incident_id = ?",
            (incident_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            observations = 1.0
        else:
            elapsed_ms = max(0, now_ms - _parse_time_ms(row["last_observed_at"]))
            observations = (
                decay_weights(
                    float(row["observations"]), 0.0, 0.0, elapsed_ms, half_life_ms
                ).joint
                + 1.0
            )

        await tx.execute(
            """
            INSERT INTO graph_node_stats (
                incident_id, scope_key, observations, last_observed_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(incident_id) DO UPDATE SET
                observations = excluded.observations,
                scope_key = excluded.scope_key,
                last_observed_at = excluded.last_observed_at
            """,
            (incident_id, scope_key, observations, observed_at),
        )

    async with tx.execute(
        "SELECT rounds, last_observed_at FROM graph_scope_stats WHERE scope_key = ?",
        (scope_key,),
    ) as cursor:
        scope_row = await cursor.fetchone()

    if scope_row is None:
        rounds = 1.0
    else:
        elapsed_ms = max(0, now_ms - _parse_time_ms(scope_row["last_observed_at"]))
        rounds = (
            decay_weights(
                float(scope_row["rounds"]), 0.0, 0.0, elapsed_ms, half_life_ms
            ).joint
            + 1.0
        )

    # observed_revision is what marks the scope dirty for the background
    # ranker. It is a counter rather than a timestamp so the decision never
    # depends on two clocks agreeing.
    await tx.execute(
        """
        INSERT INTO graph_scope_stats (
            scope_key, rounds, last_observed_at, observed_revision
        ) VALUES (?, ?, ?, 1)
        ON CONFLICT(scope_key) DO UPDATE SET
            rounds = excluded.rounds,
            last_observed_at = excluded.last_observed_at,
            observed_revision = graph_scope_stats.observed_revision + 1
        """,
        (scope_key, rounds, observed_at),
    )


async def observe_incident(
    tx: aiosqlite.Connection,
    incident_id: UUID | str,
    fingerprint_list: Iterable[str],
    *,
    half_life_ms: float = DEFAULT_HALF_LIFE_MS,
) -> tuple[EdgeUpdate, ...]:
    """Upsert directed edges without opening or committing a database connection.

    The persisted schema names the co-occurrence store ``edges``. Each related
    active incident that fired first becomes the directed source for the current
    incident, preserving lead/lag evidence for root-cause ranking. Under the
    Option B graph contract, ``edges.weight`` stores decayed_joint_weight.
    """

    current_id = str(incident_id)
    fingerprints = tuple(sorted({value for value in fingerprint_list if value}))
    if not fingerprints:
        return ()

    placeholders = ", ".join("?" for _ in fingerprints)
    active_placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
    async with tx.execute(
        f"""
        SELECT incident_id, stable_fingerprint, last_alert_at, scope_key
        FROM incidents
        WHERE stable_fingerprint IN ({placeholders})
          AND status IN ({active_placeholders})
        """,
        (*fingerprints, *_ACTIVE_STATES),
    ) as cursor:
        incidents = await cursor.fetchall()

    current = next((row for row in incidents if row["incident_id"] == current_id), None)
    if current is None:
        return ()

    current_time_ms = _parse_time_ms(current["last_alert_at"])
    observed_at = _iso_now_from_event_time(current["last_alert_at"])

    # Record the round before the edges. Every incident present is one more
    # observation of that node, and the scope has seen one more round - the
    # marginals that later let the ranker divide chance out of a co-occurrence.
    await _record_round(
        tx,
        incidents,
        scope_key=current["scope_key"],
        now_ms=current_time_ms,
        observed_at=observed_at,
        half_life_ms=half_life_ms,
    )

    updates: list[EdgeUpdate] = []
    for related in incidents:
        related_id = related["incident_id"]
        if related_id == current_id:
            continue

        related_time_ms = _parse_time_ms(related["last_alert_at"])
        if related_time_ms <= current_time_ms:
            source_id, target_id = related_id, current_id
        else:
            source_id, target_id = current_id, related_id

        existing = await _existing_edge(tx, source_id, target_id)
        if existing is None:
            updated = DecayedWeights(joint=1.0, source=1.0, target=1.0)
        else:
            weight, last_seen_at = existing
            elapsed_ms = max(0, current_time_ms - _parse_time_ms(last_seen_at))
            updated = increment_weights(
                DecayedWeights(joint=weight, source=weight, target=weight),
                elapsed_ms=elapsed_ms,
                half_life_ms=half_life_ms,
            )

        await tx.execute(
            """
            INSERT INTO edges (src_incident_id, dst_incident_id, weight, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(src_incident_id, dst_incident_id) DO UPDATE SET
                weight = excluded.weight,
                last_seen_at = excluded.last_seen_at
            """,
            (source_id, target_id, updated.joint, observed_at),
        )
        updates.append(
            EdgeUpdate(
                source_incident_id=UUID(source_id),
                target_incident_id=UUID(target_id),
                joint_weight=updated.joint,
                last_seen_at=observed_at,
            )
        )
    return tuple(updates)
