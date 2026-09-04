"""Data-only adapter between pure engine logic and Yash's transaction owner.

The wrapper reads through a transaction protocol but never opens, commits, or
rolls back a database connection. DbWriter remains the sole transaction owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Protocol
from uuid import UUID, uuid4

from .adaptive_ewma import calculate_quiet_deadline
from .dedupe import generate_fingerprint, is_exact_duplicate
from .incident_machine import transition_state

if TYPE_CHECKING:
    from src.contracts import NormalizedEvent


@dataclass(frozen=True)
class IncidentSnapshot:
    """The minimum read model the engine needs from the transaction owner."""

    incident_id: UUID
    state: str
    last_event: Mapping[str, Any]
    last_seen_ms: int
    gap_history: tuple[float, ...]
    alert_count: int


class IncidentTransaction(Protocol):
    """Read-only capability required inside Yash's existing transaction."""

    async def find_active_incident(
        self, *, scope_key: str, fingerprint: str
    ) -> IncidentSnapshot | None:
        """Return an active candidate without creating, committing, or publishing."""


@dataclass(frozen=True)
class IncidentOutcome:
    """Data returned to DbWriter for its ledger, incident, and outbox writes."""

    incident_id: UUID
    new_state: str
    quiet_at_ms: int | None
    card_changes: tuple[str, ...]
    is_critical_bypass: bool
    is_duplicate: bool
    alert_count: int
    mean_gap: float | None = None
    variance: float | None = None


def _event_time_ms(event: NormalizedEvent) -> int:
    fired_at: datetime = event.fired_at
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)
    return int(fired_at.timestamp() * 1000)


def _scope_key(event: NormalizedEvent) -> str:
    labels = event.labels
    environment = labels.get("environment", "default")
    cluster = labels.get("cluster", labels.get("namespace", "default"))
    return f"{environment}/{cluster}"


def _event_payload(event: NormalizedEvent, scope_key: str) -> dict[str, Any]:
    """Convert the shared Pydantic model into the pure dedupe input shape."""

    return {
        "scope_key": scope_key,
        "service": event.service,
        "alertname": event.alertname,
        "severity_raw": event.severity_raw,
        "status": event.status,
        "labels": dict(event.labels),
    }


def _is_protected_critical(event: NormalizedEvent) -> bool:
    """Mirror Yash's severity=critical or priority=P0 protected predicate."""

    return (
        event.severity_raw.lower() == "critical"
        or event.labels.get("priority", "").upper() == "P0"
    )


def _next_state(current_state: str, event_status: str) -> str:
    if event_status == "resolved":
        return transition_state(current_state, "RESOLVE") or current_state
    if current_state == "RESOLVED":
        return transition_state(current_state, "REOPEN") or current_state
    return current_state


async def process_event(
    transaction_obj: IncidentTransaction, normalized_event: NormalizedEvent
) -> IncidentOutcome:
    """Read an incident candidate and return the next immutable engine outcome.

    Protected critical alerts intentionally bypass lookup, deduplication, EWMA,
    and lifecycle filtering. Every non-critical event is fingerprinted, checked
    against the candidate's stable payload, and assigned a dynamic deadline.
    """

    if _is_protected_critical(normalized_event):
        return IncidentOutcome(
            incident_id=uuid4(),
            new_state="OPEN",
            quiet_at_ms=None,
            card_changes=("CRITICAL_BYPASS",),
            is_critical_bypass=True,
            is_duplicate=False,
            alert_count=1,
        )

    scope_key = _scope_key(normalized_event)
    event_payload = _event_payload(normalized_event, scope_key)
    stable_fingerprint = generate_fingerprint(event_payload)
    candidate = await transaction_obj.find_active_incident(
        scope_key=scope_key, fingerprint=stable_fingerprint
    )

    is_duplicate = candidate is not None and is_exact_duplicate(
        event_payload, dict(candidate.last_event), scope_key
    )
    event_time_ms = _event_time_ms(normalized_event)

    if not is_duplicate:
        initial_state = "RESOLVED" if normalized_event.status == "firing" else "OPEN"
        new_state = _next_state(initial_state, normalized_event.status)
        quiet = calculate_quiet_deadline([], 0.0, event_time_ms)
        return IncidentOutcome(
            incident_id=uuid4(),
            new_state=new_state,
            quiet_at_ms=int(quiet["quiet_at_ms"]),
            card_changes=("INCIDENT_OPENED", "QUIET_DEADLINE_UPDATED"),
            is_critical_bypass=False,
            is_duplicate=False,
            alert_count=1,
            mean_gap=float(quiet["mean_gap"]),
            variance=float(quiet["variance"]),
        )

    assert candidate is not None
    new_state = _next_state(candidate.state, normalized_event.status)
    last_gap = max(0.0, float(event_time_ms - candidate.last_seen_ms))
    quiet = calculate_quiet_deadline(
        list(candidate.gap_history), last_gap, event_time_ms
    )

    changes: list[str] = ["DUPLICATE_COALESCED", "QUIET_DEADLINE_UPDATED"]
    if new_state != candidate.state:
        changes.append(f"STATE_{candidate.state}_TO_{new_state}")

    return IncidentOutcome(
        incident_id=candidate.incident_id,
        new_state=new_state,
        quiet_at_ms=int(quiet["quiet_at_ms"]),
        card_changes=tuple(changes),
        is_critical_bypass=False,
        is_duplicate=True,
        alert_count=candidate.alert_count + 1,
        mean_gap=float(quiet["mean_gap"]),
        variance=float(quiet["variance"]),
    )


__all__ = ["IncidentOutcome", "IncidentSnapshot", "IncidentTransaction", "process_event"]
