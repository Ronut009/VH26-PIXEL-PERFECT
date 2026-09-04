"""GitHub webhook authentication helpers.

Webhook payload bytes must be verified before JSON decoding so intermediaries
cannot alter a delivery without invalidating GitHub's HMAC-SHA256 signature.
"""

from __future__ import annotations

import hashlib
import hmac


_SHA256_HEX_CHARACTERS = frozenset("0123456789abcdef")


def verify_github_webhook_signature(
    *, secret: str, body: bytes, signature: str | None
) -> bool:
    """Return whether a GitHub X-Hub-Signature-256 value authenticates ``body``.

    Only GitHub's current SHA-256 signature format is accepted; legacy SHA-1
    signatures intentionally fail closed.
    """

    if not secret or not signature or not signature.startswith("sha256="):
        return False
    # ``compare_digest`` raises TypeError when string inputs contain a mix of
    # ASCII and non-ASCII characters. HTTP headers are provider-controlled at
    # this boundary, so malformed values must fail closed rather than become a
    # 500 response.
    digest = signature.removeprefix("sha256=")
    if len(digest) != 64 or any(character not in _SHA256_HEX_CHARACTERS for character in digest):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def payload_digest(body: bytes) -> str:
    """Return a non-reversible audit fingerprint of a webhook body."""

    return hashlib.sha256(body).hexdigest()
