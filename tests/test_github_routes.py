"""End-to-end tests for Phase 1's signed, read-only GitHub API routes."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from unittest.mock import ANY

import pytest
from fastapi.testclient import TestClient

from src.github_integration.client import (
    BranchReference,
    GitTree,
    GitTreeEntry,
    InstallationAccessToken,
    RepositoryMetadata,
)
import src.main as main


ADMIN_TOKEN = "phase-1-admin-token"
WEBHOOK_SECRET = "phase-1-webhook-secret"


class FakeGitHubClient:
    def __init__(self) -> None:
        self.token_requests: list[tuple[int, tuple[int, ...] | None]] = []
        self.list_calls = 0
        self.snapshot_calls = 0
        self.repository = RepositoryMetadata(
            id=8123,
            owner="acme",
            name="checkout-api",
            full_name="acme/checkout-api",
            default_branch="main",
            private=True,
            html_url="https://github.com/acme/checkout-api",
        )

    async def create_installation_token(
        self, installation_id: int, *, repository_ids=None
    ) -> InstallationAccessToken:
        self.token_requests.append(
            (installation_id, tuple(repository_ids) if repository_ids is not None else None)
        )
        return InstallationAccessToken(
            token="short-lived-test-token",
            expires_at=datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
            permissions={"metadata": "read", "contents": "read"},
            repository_selection="selected",
        )

    async def list_installation_repositories(self, token) -> tuple[RepositoryMetadata, ...]:
        self.list_calls += 1
        return (self.repository,)

    async def get_repository_metadata(self, owner: str, repository: str, token) -> RepositoryMetadata:
        assert (owner, repository) == ("acme", "checkout-api")
        return self.repository

    async def get_branch(self, owner: str, repository: str, branch: str, token) -> BranchReference:
        assert (owner, repository, branch) == ("acme", "checkout-api", "main")
        return BranchReference(name="main", commit_sha="commit-abc", tree_sha="tree-abc")

    async def get_complete_tree(
        self,
        owner: str,
        repository: str,
        tree_sha: str,
        token,
        *,
        max_entries: int,
        max_tree_requests: int,
        max_tree_depth: int,
    ) -> GitTree:
        self.snapshot_calls += 1
        assert (owner, repository, tree_sha, max_entries, max_tree_requests, max_tree_depth) == (
            "acme",
            "checkout-api",
            "tree-abc",
            100_000,
            2_000,
            64,
        )
        return GitTree(
            sha="tree-abc",
            truncated=False,
            entries=(
                GitTreeEntry(
                    path="src/handlers/checkout.py",
                    mode="100644",
                    type="blob",
                    sha="blob-checkout",
                    size=42,
                    url=None,
                ),
            ),
        )


def _installation_payload(*, repositories: list[dict] | None = None) -> dict:
    return {
        "action": "created",
        "installation": {
            "id": 9988,
            "account": {"login": "acme", "type": "Organization"},
            "repository_selection": "selected",
            "permissions": {"metadata": "read", "contents": "read"},
            "suspended_at": None,
            "updated_at": "2026-09-04T10:00:00Z",
        },
        "repositories": repositories if repositories is not None else [],
    }


def _repository_payload() -> dict:
    return {
        "id": 8123,
        "name": "checkout-api",
        "full_name": "acme/checkout-api",
        "owner": {"login": "acme"},
        "default_branch": "main",
        "html_url": "https://github.com/acme/checkout-api",
        "private": True,
        "archived": False,
    }


def _signed_webhook_headers(body: bytes, *, event: str, delivery: str) -> dict[str, str]:
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-github-event": event,
        "x-github-delivery": delivery,
        "x-hub-signature-256": f"sha256={digest}",
    }


def _post_signed_webhook(client: TestClient, payload: dict, *, event: str, delivery: str):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return client.post(
        "/v1/github/webhooks",
        content=body,
        headers=_signed_webhook_headers(body, event=event, delivery=delivery),
    )


def _admin_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main.settings, "DATABASE_PATH", str(tmp_path / "alerts.db"))
    monkeypatch.setattr(main.settings, "GITHUB_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(main.settings, "GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(main.settings, "GITHUB_APP_CLIENT_ID", "")
    monkeypatch.setattr(main.settings, "GITHUB_APP_PRIVATE_KEY", "")
    monkeypatch.setattr(main.settings, "GITHUB_APP_SLUG", "pulsegraph-readonly")
    monkeypatch.setattr(main.settings, "GITHUB_MAX_TREE_ENTRIES", 100_000)

    fake_client = FakeGitHubClient()
    with TestClient(main.app) as client:
        main.app.state.github_client = fake_client
        yield client, fake_client


def test_webhook_requires_a_valid_sha256_signature(api_client) -> None:
    client, _ = api_client
    response = client.post(
        "/v1/github/webhooks",
        json=_installation_payload(),
        headers={"x-github-event": "installation", "x-github-delivery": "bad-signature"},
    )

    assert response.status_code == 403


def test_verified_installation_webhook_persists_selected_repositories(api_client) -> None:
    client, _ = api_client
    payload = _installation_payload(repositories=[_repository_payload()])

    response = _post_signed_webhook(client, payload, event="installation", delivery="delivery-1")
    repositories = client.get("/v1/github/repositories", headers=_admin_headers())

    assert response.status_code == 202
    assert response.json() == {"status": "processed"}
    assert repositories.status_code == 200
    assert repositories.json()["repositories"] == [
        {
            "repository_id": 8123,
            "installation_id": 9988,
            "owner": "acme",
            "name": "checkout-api",
            "full_name": "acme/checkout-api",
            "default_branch": "main",
            "html_url": "https://github.com/acme/checkout-api",
            "is_private": 1,
            "is_archived": 0,
            "is_selected": 1,
            "last_seen_commit_sha": None,
            "updated_at": ANY,
            "account_login": "acme",
            "installation_status": "active",
            # Many services can map to one repository, so the listing reports
            # all of them; `service` is the first, kept for the single-mapping
            # case this test covers.
            "services": [],
            "service": None,
        }
    ]


def test_verified_github_webhook_delivery_is_idempotent(api_client) -> None:
    client, _ = api_client
    payload = _installation_payload(repositories=[_repository_payload()])

    first = _post_signed_webhook(client, payload, event="installation", delivery="delivery-duplicate")
    replay = _post_signed_webhook(client, payload, event="installation", delivery="delivery-duplicate")

    assert first.json() == {"status": "processed"}
    assert replay.json() == {"status": "duplicate"}


def test_repository_admin_routes_require_the_internal_bearer_token(api_client) -> None:
    client, _ = api_client

    assert client.get("/v1/github/repositories").status_code == 401
    assert client.get("/v1/github/repositories", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/github/install-url", headers=_admin_headers()).json() == {
        "install_url": "https://github.com/apps/pulsegraph-readonly/installations/new"
    }


def test_sync_mapping_and_snapshot_are_read_only_and_pinned(api_client) -> None:
    client, fake = api_client
    install = _post_signed_webhook(
        client, _installation_payload(), event="installation", delivery="delivery-sync"
    )
    sync = client.post("/v1/github/installations/9988/sync", headers=_admin_headers())
    mapping = client.put(
        "/v1/github/service-mappings/checkout-api",
        json={"repository_id": 8123},
        headers=_admin_headers(),
    )
    snapshot = client.post("/v1/github/repositories/8123/snapshots", headers=_admin_headers())
    snapshot_view = client.get(
        f"/v1/github/snapshots/{snapshot.json()['snapshot_id']}?include_files=true",
        headers=_admin_headers(),
    )

    assert install.json() == {"status": "processed"}
    assert sync.status_code == 200
    assert sync.json() == {"status": "ok", "installation_id": 9988, "repository_ids": [8123]}
    assert mapping.json() == {
        "service": "checkout-api",
        "repository_id": 8123,
        "full_name": "acme/checkout-api",
    }
    assert snapshot.status_code == 201
    assert snapshot.json()["commit_sha"] == "commit-abc"
    assert snapshot.json()["tree_sha"] == "tree-abc"
    assert snapshot_view.json()["files"] == [
        {
            "path": "src/handlers/checkout.py",
            "blob_sha": "blob-checkout",
            "mode": "100644",
            "object_type": "blob",
            "size_bytes": 42,
        }
    ]
    assert fake.token_requests == [(9988, None), (9988, (8123,))]
    assert fake.list_calls == 1
    assert fake.snapshot_calls == 1


def test_snapshot_refuses_to_contact_github_for_an_unselected_repository(api_client) -> None:
    client, fake = api_client
    payload = _installation_payload(repositories=[_repository_payload()])
    _post_signed_webhook(client, payload, event="installation", delivery="delivery-remove")
    removed = {
        "action": "removed",
        "installation": payload["installation"],
        "repository_selection": "selected",
        "repositories_added": [],
        "repositories_removed": [_repository_payload()],
    }
    _post_signed_webhook(
        client, removed, event="installation_repositories", delivery="delivery-remove-repo"
    )

    response = client.post("/v1/github/repositories/8123/snapshots", headers=_admin_headers())

    assert response.status_code == 409
    assert fake.token_requests == []


def test_repository_selection_change_to_all_disables_the_connection(api_client) -> None:
    client, fake = api_client
    payload = _installation_payload(repositories=[_repository_payload()])
    _post_signed_webhook(client, payload, event="installation", delivery="delivery-selection-created")
    all_repositories_installation = dict(payload["installation"])
    all_repositories_installation["repository_selection"] = "all"
    broadened = {
        "action": "added",
        "installation": all_repositories_installation,
        "repository_selection": "all",
        "repositories_added": [_repository_payload()],
        "repositories_removed": [],
    }

    response = _post_signed_webhook(
        client,
        broadened,
        event="installation_repositories",
        delivery="delivery-selection-all",
    )
    snapshot = client.post("/v1/github/repositories/8123/snapshots", headers=_admin_headers())

    assert response.json() == {"status": "ignored"}
    assert snapshot.status_code == 409
    assert fake.token_requests == []
