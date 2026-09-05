"""End-to-end API tests for the bounded incident-to-patch GitHub workflow."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import INGEST_HEADERS
from src.github_integration.client import (
    BranchReference,
    GitBlob,
    GitTree,
    GitTreeEntry,
    InstallationAccessToken,
    RepositoryMetadata,
)
from src.github_integration.diagnosis import (
    DiagnosisEvidence,
    DiagnosisResult,
    ProposedFix,
    RootCauseHypothesis,
)
from src.github_integration.workspace import ChangeAction, ProposedFileChange, ProposedPatch
import src.main as main


ADMIN_TOKEN = "github-analysis-admin-token"
WEBHOOK_SECRET = "github-analysis-webhook-secret"
INSTALLATION_ID = 9988
REPOSITORY_ID = 8123
COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40
BLOB_SHA = "c" * 40
SOURCE_PATH = "src/handlers/checkout.py"
SOURCE = "def checkout(payment):\n    return charge(payment)\n"


class FakeGitHubClient:
    """A narrow fake exposing only the read methods the backend can call."""

    def __init__(self) -> None:
        self.token_requests: list[tuple[int, tuple[int, ...] | None]] = []
        self.blob_calls: list[str] = []
        self.after_blob_read = None
        self.repository = RepositoryMetadata(
            id=REPOSITORY_ID,
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
        return (self.repository,)

    async def get_repository_metadata(self, owner: str, repository: str, token) -> RepositoryMetadata:
        assert (owner, repository) == ("acme", "checkout-api")
        return self.repository

    async def get_branch(self, owner: str, repository: str, branch: str, token) -> BranchReference:
        assert (owner, repository, branch) == ("acme", "checkout-api", "main")
        return BranchReference(name="main", commit_sha=COMMIT_SHA, tree_sha=TREE_SHA)

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
        assert (owner, repository, tree_sha) == ("acme", "checkout-api", TREE_SHA)
        assert (max_entries, max_tree_requests, max_tree_depth) == (10_000, 2_000, 64)
        return GitTree(
            sha=TREE_SHA,
            truncated=False,
            entries=(
                GitTreeEntry(
                    path=SOURCE_PATH,
                    mode="100644",
                    type="blob",
                    sha=BLOB_SHA,
                    size=len(SOURCE.encode("utf-8")),
                    url=None,
                ),
            ),
        )

    async def get_blob(self, owner: str, repository: str, blob_sha: str, token) -> GitBlob:
        assert (owner, repository, blob_sha) == ("acme", "checkout-api", BLOB_SHA)
        self.blob_calls.append(blob_sha)
        callback = self.after_blob_read
        if callback is not None:
            self.after_blob_read = None
            await callback()
        return GitBlob(
            sha=BLOB_SHA,
            content=SOURCE.encode("utf-8"),
            size=len(SOURCE.encode("utf-8")),
            url=None,
        )


class GroundedDiagnosisService:
    async def diagnose(self, request) -> DiagnosisResult:
        excerpt = request.excerpts[0]
        return DiagnosisResult(
            status="diagnosed",
            provider="test-local-model",
            root_cause_hypothesis=RootCauseHypothesis(
                summary="Checkout errors escape the request boundary.",
                reasoning="The incident is a checkout 5xx burst and the pinned handler returns charge directly.",
            ),
            evidence=[
                DiagnosisEvidence(
                    kind="incident",
                    explanation="The grouped incident contains repeated checkout failures.",
                ),
                DiagnosisEvidence(
                    kind="source_excerpt",
                    explanation="The handler returns charge without a local failure boundary.",
                    file_path=excerpt.file_path,
                    blob_sha=excerpt.blob_sha,
                    start_line=2,
                    end_line=2,
                ),
            ],
            proposed_fix=ProposedFix(
                summary="Handle the expected charge failure at the checkout boundary.",
                steps=["Add a local error boundary around charge.", "Review and test the diff."],
                affected_paths=[SOURCE_PATH],
            ),
            confidence=0.82,
        )


class FakePatchProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def propose_patch(self, request, diagnosis, source_files, *, patch_id: str) -> ProposedPatch:
        self.calls += 1
        assert [item.path for item in source_files] == [SOURCE_PATH]
        assert diagnosis.proposed_fix is not None
        return ProposedPatch(
            patch_id=patch_id,
            summary="Add a checkout error boundary.",
            rationale="The pinned handler exposes charge failures directly.",
            changes=(
                ProposedFileChange(
                    path=SOURCE_PATH,
                    action=ChangeAction.UPDATE,
                    content=(
                        "def checkout(payment):\n"
                        "    try:\n"
                        "        return charge(payment)\n"
                        "    except PaymentError:\n"
                        "        return None\n"
                    ),
                    explanation="Make the failure boundary explicit for human review.",
                ),
            ),
        )


def _admin_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {ADMIN_TOKEN}"}


def _installation_payload() -> dict:
    return {
        "action": "created",
        "installation": {
            "id": INSTALLATION_ID,
            "account": {"login": "acme", "type": "Organization"},
            "repository_selection": "selected",
            "permissions": {"metadata": "read", "contents": "read"},
            "suspended_at": None,
            "updated_at": "2026-09-04T10:00:00Z",
        },
        "repositories": [],
    }


def _signed_headers(body: bytes, *, delivery: str) -> dict[str, str]:
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-github-event": "installation",
        "x-github-delivery": delivery,
        "x-hub-signature-256": f"sha256={digest}",
    }


def _connect_map_and_snapshot(client: TestClient) -> str:
    payload = _installation_payload()
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    assert client.post("/v1/github/webhooks", content=body, headers=_signed_headers(body, delivery="setup")).status_code == 202
    assert client.post(
        f"/v1/github/installations/{INSTALLATION_ID}/sync", headers=_admin_headers()
    ).status_code == 200
    assert client.put(
        "/v1/github/service-mappings/checkout-api",
        json={"repository_id": REPOSITORY_ID},
        headers=_admin_headers(),
    ).status_code == 200
    snapshot = client.post(
        f"/v1/github/repositories/{REPOSITORY_ID}/snapshots", headers=_admin_headers()
    )
    assert snapshot.status_code == 201
    return snapshot.json()["snapshot_id"]


def _ingest_checkout_incident(client: TestClient) -> str:
    payload = {
        "receiver": "pulsegraph",
        "status": "firing",
        "alerts": [
            {
                "state": "Alerting",
                "labels": {
                    "alertname": "CheckoutErrorRateHigh",
                    "severity": "critical",
                    "environment": "production",
                    "cluster": "payments-east",
                    "service": "checkout-api",
                },
                "message": "checkout 5xx errors rose after a deployment",
                "startsAt": "2026-09-04T10:15:30Z",
                "fingerprint": "checkout-alert-fingerprint",
            }
        ],
    }
    assert (
        client.post(
            "/v1/ingest/grafana", json=payload, headers=INGEST_HEADERS
        ).status_code
        == 200
    )
    incidents = client.get("/v1/incidents/recent?since=1970-01-01T00:00:00Z")
    assert incidents.status_code == 200
    return incidents.json()["incidents"][0]["incident_id"]


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main.settings, "DATABASE_PATH", str(tmp_path / "alerts.db"))
    monkeypatch.setattr(main.settings, "GITHUB_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(main.settings, "GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(main.settings, "GITHUB_APP_CLIENT_ID", "")
    monkeypatch.setattr(main.settings, "GITHUB_APP_PRIVATE_KEY", "")
    monkeypatch.setattr(main.settings, "GITHUB_MAX_TREE_ENTRIES", 10_000)

    fake_client = FakeGitHubClient()
    patch_provider = FakePatchProvider()
    with TestClient(main.app) as client:
        main.app.state.github_client = fake_client
        main.app.state.diagnosis_service = GroundedDiagnosisService()
        main.app.state.ollama_provider = patch_provider
        yield client, fake_client, patch_provider


def test_incident_diagnosis_and_patch_preview_are_bounded_and_local_only(api_client) -> None:
    client, fake_github, patch_provider = api_client
    snapshot_id = _connect_map_and_snapshot(client)
    incident_id = _ingest_checkout_incident(client)

    diagnosis = client.post(
        f"/v1/github/incidents/{incident_id}/diagnoses", headers=_admin_headers()
    )

    assert diagnosis.status_code == 201
    record = diagnosis.json()
    assert record["snapshot_id"] == snapshot_id
    assert record["diagnosis"]["status"] == "diagnosed"
    assert record["diagnosis"]["proposed_fix"]["automatically_applied"] is False
    assert record["source_context"]["excerpt_count"] == 1
    # Analysis persistence never exposes or saves the source body.
    assert SOURCE not in json.dumps(record)
    assert fake_github.blob_calls == [BLOB_SHA]

    persisted = client.get(
        f"/v1/github/analyses/{record['analysis_id']}", headers=_admin_headers()
    )
    assert persisted.status_code == 200
    assert persisted.json()["analysis_id"] == record["analysis_id"]

    preview = client.post(
        f"/v1/github/analyses/{record['analysis_id']}/patch-preview",
        headers=_admin_headers(),
    )

    assert preview.status_code == 200
    response = preview.json()
    assert response["human_review_required"] is True
    assert response["automatically_applied"] is False
    assert "--- a/src/handlers/checkout.py" in response["patch"]["unified_diff"]
    assert "PaymentError" in response["patch"]["unified_diff"]
    assert response["patch"]["metadata"]["execution_scope"] == "local_ephemeral_workspace_only"
    assert response["patch"]["metadata"]["git_commands_executed"] is False
    assert response["patch"]["metadata"]["network_accessed"] is False
    assert response["patch"]["metadata"]["source_contents_persisted"] is False
    assert patch_provider.calls == 1
    # Snapshot, diagnosis, and patch phases use restricted token reads only.
    assert fake_github.token_requests == [
        (INSTALLATION_ID, None),
        (INSTALLATION_ID, (REPOSITORY_ID,)),
        (INSTALLATION_ID, (REPOSITORY_ID,)),
        (INSTALLATION_ID, (REPOSITORY_ID,)),
    ]


def test_patch_preview_requires_a_grounded_diagnosis_and_never_calls_the_patch_model(api_client) -> None:
    client, _fake_github, patch_provider = api_client
    _connect_map_and_snapshot(client)
    incident_id = _ingest_checkout_incident(client)

    class SafeFallbackService:
        async def diagnose(self, request) -> DiagnosisResult:
            from src.github_integration.diagnosis import safe_fallback

            return safe_fallback("insufficient_evidence")

    main.app.state.diagnosis_service = SafeFallbackService()
    diagnosis = client.post(
        f"/v1/github/incidents/{incident_id}/diagnoses", headers=_admin_headers()
    )
    assert diagnosis.status_code == 201
    assert diagnosis.json()["diagnosis"]["status"] == "fallback"

    preview = client.post(
        f"/v1/github/analyses/{diagnosis.json()['analysis_id']}/patch-preview",
        headers=_admin_headers(),
    )

    assert preview.status_code == 409
    assert patch_provider.calls == 0


def test_diagnosis_fails_closed_when_repository_access_is_revoked_during_source_read(api_client) -> None:
    client, fake_github, _patch_provider = api_client
    _connect_map_and_snapshot(client)
    incident_id = _ingest_checkout_incident(client)

    async def revoke_installation() -> None:
        database = main.app.state.db
        async with database.write_lock:
            await database.writer_conn.execute(
                "UPDATE github_installations SET status = 'deleted' WHERE installation_id = ?",
                (INSTALLATION_ID,),
            )
            await database.writer_conn.commit()

    fake_github.after_blob_read = revoke_installation
    response = client.post(
        f"/v1/github/incidents/{incident_id}/diagnoses", headers=_admin_headers()
    )

    assert response.status_code == 409
    assert "mapping changed" in response.json()["detail"]
    analyses = client.get(
        f"/v1/github/incidents/{incident_id}/diagnoses", headers=_admin_headers()
    )
    assert analyses.json() == {"analyses": []}


def test_analysis_routes_require_the_admin_bearer_token(api_client) -> None:
    client, _fake_github, _patch_provider = api_client

    assert client.post("/v1/github/incidents/not-a-uuid/diagnoses").status_code == 401
    assert client.get("/v1/github/incidents/not-a-uuid/diagnoses").status_code == 401
    assert client.post("/v1/github/analyses/not-a-uuid/patch-preview").status_code == 401
