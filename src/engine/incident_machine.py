"""Pure, strict incident lifecycle transition logic."""

from typing import Literal, TypeAlias

# This lifecycle matches src.contracts.IncidentState exactly:
# ``OPEN | ACKNOWLEDGED | QUIESCENT | RESOLVED``.
IncidentState: TypeAlias = Literal["OPEN", "ACKNOWLEDGED", "QUIESCENT", "RESOLVED"]

_TRANSITIONS: dict[tuple[IncidentState, str], IncidentState] = {
    ("OPEN", "ACKNOWLEDGE"): "ACKNOWLEDGED",
    ("ACKNOWLEDGED", "QUIET_TIMEOUT"): "QUIESCENT",
    ("ACKNOWLEDGED", "QUIET_DEADLINE"): "QUIESCENT",
    ("QUIESCENT", "RESOLVE"): "RESOLVED",
    ("RESOLVED", "REOPEN"): "OPEN",
}


def transition_state(current_state: str, trigger: str) -> str | None:
    """Return the next strict lifecycle state, or ``None`` when invalid."""

    return _TRANSITIONS.get((current_state, trigger))
