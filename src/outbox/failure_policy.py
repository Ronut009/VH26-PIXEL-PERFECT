"""Classify a delivery failure as message-fatal, channel-down, or transient.

This module exists because the original worker could not tell the difference.
It treated every exception the same way: increment ``attempts``, back off, and
after ``OUTBOX_MAX_ATTEMPTS`` mark the row ``dead``. With the default backoff
of ``min(2 ** attempts, 300)`` seconds that meant a row was permanently dead
roughly 62 seconds into a Slack outage — an outage shorter than most real ones
would silently destroy every queued incident notification.

The fix is to ask a different question. A malformed block payload is *this
row's* fault and should never be retried; a 503 from slack.com is *the
channel's* fault and this row did nothing wrong, so it must not be charged an
attempt. Only the first kind can exhaust a budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import httpx


class FailureKind(str, Enum):
    """What a failed delivery says about the world."""

    # The payload or its target is wrong. Retrying sends the same bad request.
    MESSAGE_FATAL = "message_fatal"
    # The provider is unreachable or refusing traffic. Every row for this
    # channel would fail identically right now.
    CHANNEL_DOWN = "channel_down"
    # A per-request hiccup: rate limit, timeout on one call. Retry this row
    # without accusing the channel of being down until it keeps happening.
    TRANSIENT = "transient"


@dataclass(frozen=True)
class FailureVerdict:
    kind: FailureKind
    reason: str
    # Provider-supplied hint (Retry-After), in seconds, when there is one.
    retry_after_seconds: float | None = None

    @property
    def counts_against_attempts(self) -> bool:
        """Only a row's own fault burns its retry budget."""

        return self.kind is FailureKind.MESSAGE_FATAL

    @property
    def trips_breaker(self) -> bool:
        return self.kind is FailureKind.CHANNEL_DOWN


# Slack ``ok: false`` errors that mean the request itself is unacceptable.
# Retrying these forever would keep a poison row at the head of the queue.
_SLACK_MESSAGE_FATAL = frozenset(
    {
        "invalid_blocks",
        "invalid_blocks_format",
        "msg_too_long",
        "no_text",
        "channel_not_found",
        "is_archived",
        "not_in_channel",
        "message_not_found",
        "cant_update_message",
        "edit_window_closed",
    }
)

# Slack errors that mean the workspace connection itself is unusable. These are
# channel-level: no row will succeed until a human rotates the token, so the
# breaker should open rather than let every queued row burn its budget.
_SLACK_CHANNEL_DOWN = frozenset(
    {
        "token_revoked",
        "token_expired",
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "service_unavailable",
        "fatal_error",
        "internal_error",
        "ratelimited",
    }
)


class ChannelUnavailable(Exception):
    """Raised by a dispatcher that already knows the provider is unreachable."""

    def __init__(self, reason: str, retry_after_seconds: float | None = None):
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(reason)


class MessageRejected(Exception):
    """Raised when the provider rejected this specific payload, permanently."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class RateLimited(Exception):
    """Raised when the provider is healthy but asking us to slow down.

    This is deliberately not an outage. The row goes back on the queue with the
    provider's own Retry-After as its next attempt time, and the breaker stays
    closed so other traffic is unaffected.
    """

    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"rate limited, retry after {retry_after_seconds}s")


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def classify(exc: BaseException) -> FailureVerdict:
    """Map a raised exception onto what it implies about the channel."""

    if isinstance(exc, ChannelUnavailable):
        return FailureVerdict(
            FailureKind.CHANNEL_DOWN, exc.reason, exc.retry_after_seconds
        )

    if isinstance(exc, MessageRejected):
        return FailureVerdict(FailureKind.MESSAGE_FATAL, exc.reason)

    if isinstance(exc, RateLimited):
        return FailureVerdict(
            FailureKind.TRANSIENT, "rate_limited", exc.retry_after_seconds
        )

    # DNS failure, connection refused, TLS error, read timeout. The provider is
    # not answering us at all, which is the clearest possible "it is down".
    if isinstance(exc, httpx.TransportError):
        return FailureVerdict(
            FailureKind.CHANNEL_DOWN, f"transport:{type(exc).__name__}"
        )

    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = response.status_code
        if status == 429:
            # Rate limiting is the provider working correctly and asking us to
            # slow down. Honour the hint; do not declare an outage.
            return FailureVerdict(
                FailureKind.TRANSIENT, "http:429", _retry_after(response)
            )
        if status in (401, 403):
            return FailureVerdict(FailureKind.CHANNEL_DOWN, f"http:{status}")
        if 500 <= status < 600:
            return FailureVerdict(
                FailureKind.CHANNEL_DOWN, f"http:{status}", _retry_after(response)
            )
        if 400 <= status < 500:
            return FailureVerdict(FailureKind.MESSAGE_FATAL, f"http:{status}")

    # Unknown failure. Treat it as this row's problem so it can eventually be
    # dead-lettered, rather than as an outage that halts a healthy channel.
    return FailureVerdict(FailureKind.TRANSIENT, f"unknown:{type(exc).__name__}")


def classify_slack_error(error_code: str) -> FailureVerdict:
    """Classify a Slack ``ok: false`` response body error code."""

    if error_code in _SLACK_MESSAGE_FATAL:
        return FailureVerdict(FailureKind.MESSAGE_FATAL, f"slack:{error_code}")
    if error_code in _SLACK_CHANNEL_DOWN:
        return FailureVerdict(FailureKind.CHANNEL_DOWN, f"slack:{error_code}")
    return FailureVerdict(FailureKind.TRANSIENT, f"slack:{error_code}")


__all__ = [
    "ChannelUnavailable",
    "FailureKind",
    "FailureVerdict",
    "MessageRejected",
    "RateLimited",
    "classify",
    "classify_slack_error",
]
