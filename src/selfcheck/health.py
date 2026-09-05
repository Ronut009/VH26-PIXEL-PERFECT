"""Decide whether PulseGraph is actually doing its job.

The distinction this module exists for: **being alive is not being healthy.**

A liveness check proves the process is running. It proves nothing about whether
alerts are reaching anyone. PulseGraph can answer ``200 OK`` on every request
while the outbox is stalled, every channel breaker is open, or the timer worker
died hours ago - and a heartbeat driven by liveness would keep insisting all is
well the entire time. That is worse than having no heartbeat at all, because it
is an *active* all-clear rather than an absence of information.

So the verdict here is deliberately about delivery, not about uptime, and the
dead man's switch is gated on it: when this says ``UNHEALTHY`` the heartbeat
stops and the external watchdog pages.

The severity split matters too. ``DEGRADED`` means "still delivering, but
something is wrong" - a channel is down and traffic is failing over, exactly
the case the delivery-resilience work was built to survive. Paging for that
would punish the system for handling a problem correctly. ``UNHEALTHY`` is
reserved for "alerts are not getting out", which is the only condition that
justifies waking someone about the alerting system itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class SelfCheckThresholds:
    # How long a notification may sit undelivered before delivery counts as
    # stalled. Generous, because an open breaker legitimately parks rows while
    # a provider is down - this is meant to catch "nothing is draining at all",
    # not "a provider is having a bad ten minutes".
    stuck_outbox_seconds: float = 900.0
    # Dead letters are messages we gave up on. A few are a payload problem; a
    # pile means something systemic.
    dead_letter_limit: int = 10
    # Silence from every source. Genuinely ambiguous - see `observations`.
    quiet_ingest_seconds: float = 3600.0
    # Source clock offset worth reporting, mirroring the ingest-side warning.
    clock_skew_ms: int = 120_000


@dataclass(frozen=True)
class SelfCheckSignals:
    """Everything the verdict is derived from. Gathered by ``signals.py``."""

    database_reachable: bool = True
    # name -> is the asyncio task still alive
    workers: dict[str, bool] = field(default_factory=dict)
    outbox_pending: int = 0
    outbox_dead: int = 0
    oldest_pending_age_seconds: float | None = None
    open_channels: tuple[str, ...] = ()
    ongoing_outages: int = 0
    seconds_since_last_ingest: float | None = None
    worst_clock_skew_ms: int = 0
    unranked_scopes: int = 0


@dataclass(frozen=True)
class SelfCheckReport:
    verdict: Verdict
    # Why the system is not OK. Each entry is actionable on its own.
    reasons: tuple[str, ...]
    # True but not conclusive - context a human needs, that no threshold should
    # be allowed to page on.
    observations: tuple[str, ...]
    signals: SelfCheckSignals

    @property
    def should_heartbeat(self) -> bool:
        """Whether the dead man's switch may report in.

        Degraded still beats: alerts are getting out, and the delivery plane is
        handling a failure the way it was designed to. Only a system that
        cannot deliver goes silent.
        """

        return self.verdict is not Verdict.UNHEALTHY


def evaluate(
    signals: SelfCheckSignals, thresholds: SelfCheckThresholds | None = None
) -> SelfCheckReport:
    """Turn raw signals into a verdict. Pure, so every rule can be tested."""

    limits = thresholds or SelfCheckThresholds()
    fatal: list[str] = []
    degraded: list[str] = []
    observations: list[str] = []

    # ── cannot deliver ────────────────────────────────────────────────────

    if not signals.database_reachable:
        fatal.append(
            "database is unreachable; nothing can be recorded or delivered"
        )

    dead_workers = sorted(name for name, alive in signals.workers.items() if not alive)
    if dead_workers:
        # A dead background task is invisible from outside: HTTP keeps
        # answering, ingest keeps accepting, and nothing is ever sent.
        fatal.append(
            f"background worker(s) not running: {', '.join(dead_workers)}"
        )

    oldest = signals.oldest_pending_age_seconds
    if oldest is not None and oldest > limits.stuck_outbox_seconds:
        fatal.append(
            f"oldest undelivered notification is {int(oldest)}s old "
            f"(limit {int(limits.stuck_outbox_seconds)}s); delivery is stalled"
        )

    # ── delivering, but something is wrong ────────────────────────────────

    if signals.open_channels:
        degraded.append(
            f"channel breaker open: {', '.join(signals.open_channels)}"
        )

    if signals.outbox_dead > limits.dead_letter_limit:
        degraded.append(
            f"{signals.outbox_dead} notifications dead-lettered "
            f"(limit {limits.dead_letter_limit})"
        )

    if abs(signals.worst_clock_skew_ms) > limits.clock_skew_ms:
        degraded.append(
            f"a source clock is {abs(signals.worst_clock_skew_ms) // 1000}s out; "
            "its reported timestamps cannot be trusted"
        )

    # ── true, but not a conclusion ────────────────────────────────────────

    quiet = signals.seconds_since_last_ingest
    if quiet is not None and quiet > limits.quiet_ingest_seconds:
        # Deliberately not a failure condition. A quiet night and a severed
        # intake path look identical from in here, and guessing wrong in
        # either direction is costly: page on every quiet night and the
        # heartbeat becomes noise; treat a severed intake as calm and the
        # blind spot is total. Only something outside can tell them apart,
        # which is the whole argument for an external watchdog.
        observations.append(
            f"no alerts received for {int(quiet)}s - either genuinely quiet or "
            "the intake path is broken; this cannot be told apart from inside"
        )

    if signals.ongoing_outages:
        observations.append(
            f"{signals.ongoing_outages} channel outage(s) currently open"
        )

    if signals.outbox_pending:
        observations.append(f"{signals.outbox_pending} notification(s) queued")

    if signals.unranked_scopes:
        observations.append(
            f"{signals.unranked_scopes} scope(s) awaiting root-cause ranking"
        )

    if fatal:
        verdict = Verdict.UNHEALTHY
    elif degraded:
        verdict = Verdict.DEGRADED
    else:
        verdict = Verdict.OK

    return SelfCheckReport(
        verdict=verdict,
        reasons=tuple(fatal + degraded),
        observations=tuple(observations),
        signals=signals,
    )


__all__ = [
    "SelfCheckReport",
    "SelfCheckSignals",
    "SelfCheckThresholds",
    "Verdict",
    "evaluate",
]
