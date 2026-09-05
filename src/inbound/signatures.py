"""Signature verification for provider callbacks.

These endpoints are public: anything on the internet can POST to them. An
unverified callback endpoint would let anyone resolve any incident by guessing
a UUID, which is a worse failure than the outage handling exists to survive -
it silences a real emergency rather than delaying it.

Every check here is constant-time and every failure is indistinguishable to the
caller, so these functions never explain *why* a signature was rejected to
anyone but our own logs.
"""

from __future__ import annotations

import hashlib
import hmac
import time


class SignatureError(Exception):
    """Raised when a callback cannot be proven to come from the provider."""


# Slack signs `v0:{timestamp}:{body}`. The timestamp window exists to stop a
# captured request being replayed later; five minutes is Slack's own guidance.
SLACK_REPLAY_WINDOW_SECONDS = 300


def verify_slack(
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    *,
    now: float | None = None,
) -> None:
    """Verify a Slack request signature, or raise ``SignatureError``."""

    if not signing_secret:
        raise SignatureError("slack signing secret is not configured")
    if not timestamp or not signature:
        raise SignatureError("missing slack signature headers")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise SignatureError("malformed slack timestamp") from exc

    current = time.time() if now is None else now
    if abs(current - sent_at) > SLACK_REPLAY_WINDOW_SECONDS:
        raise SignatureError("slack timestamp outside replay window")

    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    expected = (
        "v0="
        + hmac.new(
            signing_secret.encode("utf-8"), basestring, hashlib.sha256
        ).hexdigest()
    )

    if not hmac.compare_digest(expected, signature):
        raise SignatureError("slack signature mismatch")


def verify_pagerduty(secret: str, signature_header: str, body: bytes) -> None:
    """Verify a PagerDuty v3 webhook signature, or raise ``SignatureError``.

    PagerDuty sends a comma-separated list of signatures so a secret can be
    rotated without dropping deliveries: during rotation both the old and the
    new signature are present, and matching *any* of them is correct.
    """

    if not secret:
        raise SignatureError("pagerduty webhook secret is not configured")
    if not signature_header:
        raise SignatureError("missing pagerduty signature header")

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    for candidate in signature_header.split(","):
        candidate = candidate.strip()
        if not candidate.startswith("v1="):
            continue
        if hmac.compare_digest(candidate[3:], expected):
            return

    raise SignatureError("pagerduty signature mismatch")


def payload_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "SLACK_REPLAY_WINDOW_SECONDS",
    "SignatureError",
    "payload_digest",
    "verify_pagerduty",
    "verify_slack",
]
