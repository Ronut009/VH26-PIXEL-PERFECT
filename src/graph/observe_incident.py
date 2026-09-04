"""Single-transaction directed co-occurrence edge updates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

import aiosqlite

from .edge_decay import DecayedWeights, increment_weights

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
    incident, preserving lead/lag evidence for root-cause ranking.
    """

    current_id = str(incident_id)
    fingerprints = tuple(sorted({value for value in fingerprint_list if value}))
    if not fingerprints:
        return ()

    placeholders = ", ".join("?" for _ in fingerprints)
    active_placeholders = ", ".join("?" for _ in _ACTIVE_STATES)
    async with tx.execute(
        f"""
        SELECT incident_id, stable_fingerprint, last_alert_at
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
