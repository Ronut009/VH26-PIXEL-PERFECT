"""Pure, strict incident lifecycle transition logic."""

from typing import Literal, TypeAlias

# This uppercase lifecycle is the Phase 2 engine contract. Phase 4 must map it
# to src.contracts.IncidentDecision.status, which currently uses lower-case
# ``new | active | quiet | resolved`` and has no acknowledged status.
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
