"""Signature verification tests using GitHub's published validation vector."""

from src.github_integration.webhooks import (
    payload_digest,
    verify_github_webhook_signature,
)


def test_verifies_githubs_documented_sha256_example() -> None:
    assert verify_github_webhook_signature(
        secret="It's a Secret to Everybody",
        body=b"Hello, World!",
        signature="sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17",
    )


def test_rejects_missing_legacy_and_invalid_signatures() -> None:
    body = b'{"installation":{"id":1}}'
    assert not verify_github_webhook_signature(secret="secret", body=body, signature=None)
    assert not verify_github_webhook_signature(
        secret="secret", body=body, signature="sha1=legacy"
    )
    assert not verify_github_webhook_signature(
        secret="secret", body=body, signature="sha256=" + "0" * 64
    )
    assert not verify_github_webhook_signature(
        secret="secret", body=body, signature="sha256=" + "é" * 32
    )


def test_audit_digest_does_not_retain_webhook_body() -> None:
    body = b'{"token":"never-store-this"}'
    digest = payload_digest(body)

    assert len(digest) == 64
    assert "never-store-this" not in digest
