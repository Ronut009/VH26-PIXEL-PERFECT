"""Pure, strict incident lifecycle transition logic."""

from typing import Literal, TypeAlias

# This lifecycle matches src.contracts.IncidentState exactly:
# ``OPEN | ACKNOWLEDGED | QUIESCENT | RESOLVED``.
IncidentState: TypeAlias = Literal["OPEN", "ACKNOWLEDGED", "QUIESCENT", "RESOLVED"]

# The lifecycle was originally forward-only: RESOLVE was reachable from
# QUIESCENT alone. That modelled the happy path - an incident goes quiet, then
# closes - but it could not express the most common real ending of all, which
# is that somebody fixed the problem while it was still firing.
#
# Because a new incident is created directly in ACKNOWLEDGED, every live
# incident sat in a state with no RESOLVE edge, so a `resolved` webhook from
# Prometheus, or an operator resolving in PagerDuty, was silently dropped and
# the incident stayed open forever.
#
# RESOLVE is therefore valid from every active state. Resolution is an
# assertion about the world - the alert stopped, or a human says it is fixed -
# and the world does not have to route through QUIESCENT first.
_TRANSITIONS: dict[tuple[IncidentState, str], IncidentState] = {
    ("OPEN", "ACKNOWLEDGE"): "ACKNOWLEDGED",
    ("ACKNOWLEDGED", "QUIET_TIMEOUT"): "QUIESCENT",
    ("ACKNOWLEDGED", "QUIET_DEADLINE"): "QUIESCENT",
    ("OPEN", "RESOLVE"): "RESOLVED",
    ("ACKNOWLEDGED", "RESOLVE"): "RESOLVED",
    ("QUIESCENT", "RESOLVE"): "RESOLVED",
    ("RESOLVED", "REOPEN"): "OPEN",
}


def transition_state(current_state: str, trigger: str) -> str | None:
    """Return the next strict lifecycle state, or ``None`` when invalid."""

    return _TRANSITIONS.get((current_state, trigger))
