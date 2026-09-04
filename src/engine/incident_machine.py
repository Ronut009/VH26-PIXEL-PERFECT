"""Pure incident lifecycle transition contract."""

from typing import Literal, TypeAlias

# src.contracts.IncidentDecision.status uses precisely these four values.
# The shared contract does not currently export an IncidentState alias.
IncidentState: TypeAlias = Literal["new", "active", "quiet", "resolved"]


def transition_state(current: IncidentState, trigger: str) -> IncidentState | None:
    """Return the next state for ``trigger``, or ``None`` when invalid.

    Transition mapping is intentionally deferred to Phase 3 after Phase 2
    locks its acceptance tests.
    """

    raise NotImplementedError("Phase 3: implement lifecycle transitions")
