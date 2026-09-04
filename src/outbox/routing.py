"""Severity-driven priority and failover routing.

Two decisions live here, both of which the queue itself cannot make:

1. **Priority.** A backlog drained strictly by ``outbox_id`` delivers a
   week-old ``low`` notification before a ``critical`` page that arrived during
   the outage. Priority is derived from severity at enqueue time so the drain
   order after recovery matches what an on-call engineer actually needs.

2. **Failover.** When a channel is declared down, waiting is only acceptable
   for alerts that can wait. Anything at or above ``FAILOVER_MIN_PRIORITY``
   is re-routed onto an independent provider immediately, rather than sitting
   in a queue nobody is watching.

Failover targets are deliberately chosen to share as little failure domain as
possible with the primary: a different vendor, a different DNS zone, and in the
dashboard's case a path with no third party in it at all.
"""

from __future__ import annotations

from dataclasses import dataclass

# Lower drains first.
PRIORITY_BY_SEVERITY: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}
DEFAULT_PRIORITY = PRIORITY_BY_SEVERITY["medium"]

# Only critical and high are worth waking a secondary path for. Paging someone
# over a `medium` because Slack is briefly unreachable trades one kind of alert
# fatigue for a worse one.
FAILOVER_MIN_PRIORITY = PRIORITY_BY_SEVERITY["high"]

# Ordered fallbacks per primary channel. The worker walks this list and takes
# the first entry whose own breaker is not open.
FAILOVER_CHAIN: dict[str, tuple[str, ...]] = {
    # PagerDuty is a separate company on separate infrastructure, so a Slack
    # outage says nothing about it. Email is the last resort before the
    # dashboard, which is always reachable because we serve it ourselves.
    "slack": ("pagerduty", "email"),
    # If PagerDuty is the one that is down, Slack still reaches the channel
    # where the team already is.
    "pagerduty": ("slack", "email"),
    "email": (),
}


def priority_for(severity: str | None) -> int:
    return PRIORITY_BY_SEVERITY.get((severity or "").lower(), DEFAULT_PRIORITY)


def is_failover_worthy(priority: int) -> bool:
    return priority <= FAILOVER_MIN_PRIORITY


@dataclass(frozen=True)
class FailoverTarget:
    channel: str
    action: str


def select_failover(
    origin_channel: str, action: str, available: set[str]
) -> FailoverTarget | None:
    """Pick the first healthy fallback for a channel that is down."""

    for candidate in FAILOVER_CHAIN.get(origin_channel, ()):
        if candidate in available:
            return FailoverTarget(candidate, _translate_action(candidate, action))
    return None


def _translate_action(channel: str, action: str) -> str:
    """Map an intent onto what the target channel can express.

    PagerDuty has no notion of editing a card: an ``update`` for an incident it
    has never seen has to become a trigger, and PagerDuty's own ``dedup_key``
    (the incident id) makes a repeat trigger idempotent on their side. That is
    what stops failover from double-paging an incident that critical-bypass
    already sent there.
    """

    if channel == "pagerduty" and action == "update":
        return "create"
    return action


__all__ = [
    "DEFAULT_PRIORITY",
    "FAILOVER_CHAIN",
    "FAILOVER_MIN_PRIORITY",
    "PRIORITY_BY_SEVERITY",
    "FailoverTarget",
    "is_failover_worthy",
    "priority_for",
    "select_failover",
]
