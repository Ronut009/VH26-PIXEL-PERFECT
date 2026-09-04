"""Focused contract tests for the GitHub App read-only integration client."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from src.github_integration.client import (
    GitHubAuthenticationError,
    GitHubRateLimitError,
    GitHubReadOnlyClient,
    GitHubTimeoutError,
    GitHubTreeLimitExceeded,
)


API_BASE = "https://github.test/api/v3"


@pytest.fixture
def app_keys() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_key = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_key


async def _make_client(
    handler: httpx.MockTransport.Handler,
    private_key: bytes,
) -> tuple[GitHubReadOnlyClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        GitHubReadOnlyClient(
            app_id=12345,
            private_key_pem=private_key,
            base_url=API_BASE,
            timeout=3.0,
            http_client=http_client,
        ),
        http_client,
    )


def _repository_payload() -> dict[str, Any]:
    return {
        "id": 987,
        "name": "pulsegraph",
        "full_name": "acme/pulsegraph",
        "owner": {"login": "acme"},
        "default_branch": "main",
        "private": True,
        "html_url": "https://github.com/acme/pulsegraph",
    }


def _tree_entry(path: str, entry_type: str, sha: str, *, size: int | None = None) -> dict[str, Any]:
    return {
        "path": path,
        "mode": "040000" if entry_type == "tree" else "100644",
        "type": entry_type,
        "sha": sha,
        "size": size,
        "url": f"https://api.github.com/example/{sha}",
    }


def test_create_app_jwt_is_a_valid_rs256_github_app_token(app_keys) -> None:
    private_key, public_key = app_keys
    client = GitHubReadOnlyClient(app_id=12345, private_key_pem=private_key)
    now = datetime(2030, 5, 1, 12, 0, tzinfo=timezone.utc)

    token = client.create_app_jwt(now=now)

    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {
        "iat": int(now.timestamp()) - 60,
        "exp": int(now.timestamp()) + 540,
        "iss": "12345",
    }


@pytest.mark.asyncio
async def test_read_only_client_authenticates_and_fetches_source_snapshot(app_keys) -> None:
    private_key, _ = app_keys
    requests: list[httpx.Request] = []
    source = b"print('ok')\n"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["x-github-api-version"] == "2022-11-28"

        if path == "/api/v3/app/installations/321/access_tokens":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "permissions": {"contents": "read"},
                "repository_ids": [987],
            }
            encoded_jwt = request.headers["authorization"].removeprefix("Bearer ")
            assert jwt.get_unverified_header(encoded_jwt)["alg"] == "RS256"
            return httpx.Response(
                201,
                json={
                    "token": "installation-secret",
                    "expires_at": "2030-05-01T13:00:00Z",
                    "permissions": {"contents": "read", "metadata": "read"},
                    "repository_selection": "selected",
                },
            )
        assert request.headers["authorization"] == "Bearer installation-secret"

        if path == "/api/v3/installation/repositories":
            assert dict(request.url.params) == {"per_page": "100", "page": "1"}
            return httpx.Response(200, json={"total_count": 1, "repositories": [_repository_payload()]})
        if path == "/api/v3/repos/acme/pulsegraph":
            return httpx.Response(200, json=_repository_payload())
        if path == "/api/v3/repos/acme/pulsegraph/branches/main":
            return httpx.Response(
                200,
                json={
                    "name": "main",
                    "commit": {
                        "sha": "commit-sha",
                        "commit": {"tree": {"sha": "tree-sha"}},
                    },
                },
            )
        if path == "/api/v3/repos/acme/pulsegraph/commits/main":
            return httpx.Response(200, json={"sha": "commit-sha"})
        if path == "/api/v3/repos/acme/pulsegraph/git/trees/tree-sha":
            assert dict(request.url.params) == {"recursive": "1"}
            return httpx.Response(
                200,
                json={
                    "sha": "tree-sha",
                    "truncated": False,
                    "tree": [_tree_entry("src/main.py", "blob", "blob-sha", size=len(source))],
                },
            )
        if path == "/api/v3/repos/acme/pulsegraph/git/blobs/blob-sha":
            return httpx.Response(
                200,
                json={
                    "sha": "blob-sha",
                    "size": len(source),
                    "encoding": "base64",
                    "content": base64.b64encode(source).decode("ascii"),
                    "url": "https://api.github.com/example/blob-sha",
                },
            )
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")

    client, http_client = await _make_client(handler, private_key)
    try:
        access_token = await client.create_installation_token(321, repository_ids=[987])
        repositories = await client.list_installation_repositories(access_token)
        metadata = await client.get_repository_metadata("acme", "pulsegraph", access_token)
        branch = await client.get_branch("acme", "pulsegraph", "main", access_token)
        commit_sha = await client.resolve_ref("acme", "pulsegraph", "main", access_token)
        tree = await client.get_complete_tree("acme", "pulsegraph", branch.tree_sha, access_token)
        blob = await client.get_blob("acme", "pulsegraph", "blob-sha", access_token)
    finally:
        await http_client.aclose()

    assert access_token.expires_at == datetime(2030, 5, 1, 13, 0, tzinfo=timezone.utc)
    assert access_token.permissions == {"contents": "read", "metadata": "read"}
    assert repositories == (metadata,)
    assert metadata.default_branch == "main"
    assert branch.commit_sha == commit_sha == "commit-sha"
    assert branch.tree_sha == tree.sha == "tree-sha"
    assert tree.truncated is False
    assert tree.entries[0].path == "src/main.py"
    assert blob.content == source
    assert blob.text() == "print('ok')\n"
    # The only POST is GitHub's required access-token exchange; all source
    # retrieval calls are GET and no mutation verb can reach the transport.
    assert [request.method for request in requests] == ["POST", "GET", "GET", "GET", "GET", "GET", "GET"]
    assert set(request.method for request in requests) <= {"GET", "POST"}


@pytest.mark.asyncio
async def test_get_complete_tree_falls_back_when_recursive_result_is_truncated(app_keys) -> None:
    private_key, _ = app_keys
    calls: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, str(request.url.params)))
        assert request.headers["authorization"] == "Bearer installation-token"
        if dict(request.url.params) == {"recursive": "1"}:
            return httpx.Response(
                200,
                json={
                    "sha": "root-tree",
                    "truncated": True,
                    "tree": [_tree_entry("partial.txt", "blob", "not-used", size=1)],
                },
            )
        if request.url.path.endswith("/trees/root-tree"):
            return httpx.Response(
                200,
                json={
                    "sha": "root-tree",
                    "truncated": False,
                    "tree": [
                        _tree_entry("README.md", "blob", "readme-blob", size=2),
                        _tree_entry("src", "tree", "src-tree"),
                    ],
                },
            )
        if request.url.path.endswith("/trees/src-tree"):
            return httpx.Response(
                200,
                json={
                    "sha": "src-tree",
                    "truncated": False,
                    "tree": [_tree_entry("app.py", "blob", "app-blob", size=5)],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client, http_client = await _make_client(handler, private_key)
    try:
        tree = await client.get_complete_tree(
            "acme", "pulsegraph", "root-tree", "installation-token", max_entries=10
        )
    finally:
        await http_client.aclose()

    assert tree.truncated is False
    assert [entry.path for entry in tree.entries] == ["README.md", "src", "src/app.py"]
    assert calls == [
        ("GET", "/api/v3/repos/acme/pulsegraph/git/trees/root-tree", "recursive=1"),
        ("GET", "/api/v3/repos/acme/pulsegraph/git/trees/root-tree", ""),
        ("GET", "/api/v3/repos/acme/pulsegraph/git/trees/src-tree", ""),
    ]


@pytest.mark.asyncio
async def test_get_complete_tree_raises_at_the_configured_safe_bound(app_keys) -> None:
    private_key, _ = app_keys

    def handler(request: httpx.Request) -> httpx.Response:
        if dict(request.url.params) == {"recursive": "1"}:
            return httpx.Response(200, json={"sha": "tree", "truncated": True, "tree": []})
        return httpx.Response(
            200,
            json={
                "sha": "tree",
                "truncated": False,
                "tree": [
                    _tree_entry("one.py", "blob", "one", size=1),
                    _tree_entry("two.py", "blob", "two", size=1),
                ],
            },
        )

    client, http_client = await _make_client(handler, private_key)
    try:
        with pytest.raises(GitHubTreeLimitExceeded, match="configured 1 entries"):
            await client.get_complete_tree("acme", "pulsegraph", "tree", "token", max_entries=1)
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_errors_are_typed_and_never_echo_the_bearer_token(app_keys) -> None:
    private_key, _ = app_keys

    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": "Bad credentials"},
            headers={"x-github-request-id": "request-123"},
        )

    client, http_client = await _make_client(unauthorized, private_key)
    try:
        with pytest.raises(GitHubAuthenticationError) as error:
            await client.get_repository("acme", "pulsegraph", "very-secret-token")
    finally:
        await http_client.aclose()

    assert error.value.status_code == 401
    assert error.value.request_id == "request-123"
    assert "very-secret-token" not in str(error.value)


@pytest.mark.asyncio
async def test_rate_limits_and_transport_timeouts_have_specific_errors(app_keys) -> None:
    private_key, _ = app_keys

    def rate_limited(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"retry-after": "12", "x-ratelimit-remaining": "0"},
        )

    client, http_client = await _make_client(rate_limited, private_key)
    try:
        with pytest.raises(GitHubRateLimitError) as error:
            await client.get_repository("acme", "pulsegraph", "token")
    finally:
        await http_client.aclose()

    assert error.value.retry_after_seconds == 12

    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout")

    client, http_client = await _make_client(timeout, private_key)
    try:
        with pytest.raises(GitHubTimeoutError):
            await client.get_repository("acme", "pulsegraph", "token")
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_token_exchange_rejects_any_returned_write_permission(app_keys) -> None:
    private_key, _ = app_keys

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "token": "do-not-return-this",
                "expires_at": "2030-05-01T13:00:00Z",
                "permissions": {"contents": "write"},
                "repository_selection": "selected",
            },
        )

    client, http_client = await _make_client(handler, private_key)
    try:
        with pytest.raises(GitHubAuthenticationError, match="Metadata/Contents") as error:
            await client.create_installation_token(321)
    finally:
        await http_client.aclose()

    assert "do-not-return-this" not in str(error.value)


@pytest.mark.asyncio
async def test_token_exchange_rejects_all_repositories_scope(app_keys) -> None:
    private_key, _ = app_keys

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "token": "do-not-return-this",
                "expires_at": "2030-05-01T13:00:00Z",
                "permissions": {"contents": "read", "metadata": "read"},
                "repository_selection": "all",
            },
        )

    client, http_client = await _make_client(handler, private_key)
    try:
        with pytest.raises(GitHubAuthenticationError, match="selected repositories") as error:
            await client.create_installation_token(321)
    finally:
        await http_client.aclose()

    assert "do-not-return-this" not in str(error.value)
