"""Mocked contract tests for the local-only Ollama diagnosis adapter."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

import httpx
import pytest

from src.github_integration.diagnosis import (
    DiagnosisEvidence,
    DiagnosisIncidentContext,
    DiagnosisRequest,
    DiagnosisResult,
    DiagnosisService,
    ProposedFix,
    RootCauseHypothesis,
    SourceExcerpt,
    SourceSnapshotReference,
)
from src.github_integration.ollama_provider import (
    OllamaLocalAPIError,
    OllamaLocalConfigurationError,
    OllamaLocalLimits,
    OllamaLocalProvider,
    OllamaLocalResponseError,
    PatchSourceFile,
)


INCIDENT_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40
BLOB_SHA = "c" * 40
SOURCE_PATH = "src/handlers/checkout.py"
SOURCE_CONTENT = "def handle_checkout(request):\n    return charge(request)\n"


def _request() -> DiagnosisRequest:
    return DiagnosisRequest(
        incident=DiagnosisIncidentContext(
            incident_id=INCIDENT_ID,
            service="checkout-api",
            alertname="CheckoutErrorRateHigh",
            severity="critical",
            status="firing",
            scope_key="production:payments-east",
            alert_count=100,
            message="checkout 5xx rate rose after deployment",
            labels={"environment": "production", "cluster": "payments-east"},
        ),
        snapshot=SourceSnapshotReference(
            snapshot_id=SNAPSHOT_ID,
            repository_id=8123,
            repository_full_name="acme/checkout-api",
            commit_sha=COMMIT_SHA,
            tree_sha=TREE_SHA,
        ),
        excerpts=[
            SourceExcerpt(
                file_path=SOURCE_PATH,
                blob_sha=BLOB_SHA,
                start_line=40,
                end_line=48,
                content=SOURCE_CONTENT,
                language="python",
            )
        ],
    )


def _diagnosis_payload(*, source_path: str = SOURCE_PATH, blob_sha: str = BLOB_SHA) -> dict[str, object]:
    return {
        "status": "diagnosed",
        "root_cause_hypothesis": {
            "summary": "The checkout handler lets payment failures escape as 5xx responses.",
            "reasoning": "The incident reports checkout 5xx errors and the cited handler calls charge without a local failure boundary.",
        },
        "evidence": [
            {
                "kind": "incident",
                "explanation": "The coalesced incident contains 100 checkout 5xx alerts.",
            },
            {
                "kind": "source_excerpt",
                "explanation": "The supplied charge call lacks local expected-failure handling.",
                "file_path": source_path,
                "blob_sha": blob_sha,
                "start_line": 41,
                "end_line": 42,
            },
        ],
        "proposed_fix": {
            "summary": "Handle the known payment failure at the checkout boundary.",
            "steps": [
                "Catch the expected payment failure around charge.",
                "Review the resulting local patch before merging.",
            ],
            "affected_paths": [source_path],
            "requires_human_review": True,
            "automatically_applied": False,
        },
        "confidence": 0.74,
    }


def _response(content: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "qwen2.5-coder:7b",
            "message": {"role": "assistant", "content": json.dumps(content)},
            "done": True,
        },
    )


def _provider(handler: httpx.MockTransport.Handler, *, limits: OllamaLocalLimits | None = None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaLocalProvider(
        "qwen2.5-coder:7b",
        base_url="http://127.0.0.1:11434",
        limits=limits,
        http_client=client,
    )
    return provider, client


@pytest.mark.asyncio
async def test_diagnosis_uses_only_local_non_streaming_schema_bound_chat() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url == httpx.URL("http://127.0.0.1:11434/api/chat")
        assert request.headers["content-type"] == "application/json"
        body = json.loads(request.content)
        assert body["model"] == "qwen2.5-coder:7b"
        assert body["stream"] is False
        assert body["options"] == {"temperature": 0, "num_predict": 2048}
        assert body["format"]["type"] == "object"
        assert body["format"]["additionalProperties"] is False
        assert body["messages"][0]["role"] == "system"
        user_payload = json.loads(body["messages"][1]["content"])
        assert user_payload["task"] == "diagnose_incident"
        assert user_payload["incident"]["service"] == "checkout-api"
        assert user_payload["excerpts"][0]["file_path"] == SOURCE_PATH
        return _response(_diagnosis_payload())

    provider, client = _provider(handler)
    try:
        result = await DiagnosisService(provider).diagnose(_request())
    finally:
        await client.aclose()

    assert len(requests) == 1
    assert result.status == "diagnosed"
    assert result.provider == "ollama-local"
    assert result.confidence == 0.74
    assert result.proposed_fix is not None
    assert result.proposed_fix.requires_human_review is True
    assert result.proposed_fix.automatically_applied is False


@pytest.mark.asyncio
async def test_patch_proposal_injects_hash_preconditions_and_only_allows_grounded_editable_paths() -> None:
    captured: list[dict[str, object]] = []
    patch_response = {
        "summary": "Handle the payment failure at the checkout boundary.",
        "rationale": "The cited charge call is the source evidence for the failure path.",
        "changes": [
            {
                "action": "update",
                "path": SOURCE_PATH,
                "content": "def handle_checkout(request):\n    try:\n        return charge(request)\n    except PaymentError:\n        return payment_failed_response()\n",
                "explanation": "Add an explicit known-payment-failure boundary.",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        assert body["stream"] is False
        assert body["format"]["additionalProperties"] is False
        assert "patch_id" not in body["format"]["properties"]
        user_payload = json.loads(body["messages"][1]["content"])
        assert user_payload["task"] == "propose_local_patch"
        assert user_payload["editable_files"] == [
            {"path": SOURCE_PATH, "blob_sha": BLOB_SHA, "content": SOURCE_CONTENT}
        ]
        return _response(patch_response)

    provider, client = _provider(handler)
    request = _request()
    diagnosis = DiagnosisResult(
        status="diagnosed",
        provider="trusted-local-result",
        root_cause_hypothesis=RootCauseHypothesis(
            summary="Payment errors escape the checkout handler.",
            reasoning="The bounded source excerpt calls charge with no local failure boundary.",
        ),
        evidence=[
            DiagnosisEvidence(
                kind="source_excerpt",
                explanation="The charge call is contained in the supplied excerpt.",
                file_path=SOURCE_PATH,
                blob_sha=BLOB_SHA,
                start_line=41,
                end_line=42,
            )
        ],
        proposed_fix=ProposedFix(
            summary="Handle expected payment errors locally.",
            steps=["Add a known-payment-error handler."],
            affected_paths=[SOURCE_PATH],
        ),
        confidence=0.8,
    )
    try:
        proposal = await provider.propose_patch(
            request,
            diagnosis,
            [PatchSourceFile(path=SOURCE_PATH, blob_sha=BLOB_SHA, content=SOURCE_CONTENT)],
            patch_id="incident-1042-payment-boundary",
        )
    finally:
        await client.aclose()

    assert len(captured) == 1
    assert proposal.patch_id == "incident-1042-payment-boundary"
    assert proposal.changes[0].action.value == "update"
    assert proposal.changes[0].path == SOURCE_PATH
    assert proposal.changes[0].expected_sha256 == hashlib.sha256(SOURCE_CONTENT.encode("utf-8")).hexdigest()
    assert proposal.changes[0].content == patch_response["changes"][0]["content"]


@pytest.mark.asyncio
async def test_ungrounded_diagnosis_and_non_editable_patch_paths_are_rejected() -> None:
    def diagnosis_handler(request: httpx.Request) -> httpx.Response:
        return _response(_diagnosis_payload(source_path="src/not-supplied.py", blob_sha="d" * 40))

    provider, client = _provider(diagnosis_handler)
    try:
        with pytest.raises(OllamaLocalResponseError, match="grounded JSON contract"):
            await provider.diagnose(_request())
    finally:
        await client.aclose()

    request = _request()
    grounded_diagnosis = DiagnosisResult(
        status="diagnosed",
        provider="test",
        root_cause_hypothesis=RootCauseHypothesis(
            summary="Payment errors escape checkout.",
            reasoning="The supplied charge call has no local failure boundary.",
        ),
        evidence=[
            DiagnosisEvidence(
                kind="source_excerpt",
                explanation="This is within the supplied source excerpt.",
                file_path=SOURCE_PATH,
                blob_sha=BLOB_SHA,
                start_line=41,
                end_line=42,
            )
        ],
        proposed_fix=ProposedFix(
            summary="Handle payment errors.",
            steps=["Catch expected payment failures."],
            affected_paths=[SOURCE_PATH],
        ),
        confidence=0.8,
    )

    def patch_handler(request: httpx.Request) -> httpx.Response:
        return _response(
            {
                "summary": "Unsafe patch target.",
                "changes": [
                    {
                        "action": "update",
                        "path": "src/not-supplied.py",
                        "content": "unsafe\n",
                        "explanation": "This must be rejected.",
                    }
                ],
            }
        )

    provider, client = _provider(patch_handler)
    try:
        with pytest.raises(OllamaLocalResponseError, match="non-editable"):
            await provider.propose_patch(
                request,
                grounded_diagnosis,
                [PatchSourceFile(path=SOURCE_PATH, blob_sha=BLOB_SHA, content=SOURCE_CONTENT)],
                patch_id="safe-patch",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unpinned_patch_source_is_rejected_before_any_model_request() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("an unpinned source file must not be sent to Ollama")

    provider, client = _provider(handler)
    request = _request()
    diagnosis = DiagnosisResult(
        status="diagnosed",
        provider="test",
        root_cause_hypothesis=RootCauseHypothesis(
            summary="Payment errors escape checkout.",
            reasoning="The supplied charge call has no local failure boundary.",
        ),
        evidence=[
            DiagnosisEvidence(
                kind="source_excerpt",
                explanation="This is within the supplied source excerpt.",
                file_path=SOURCE_PATH,
                blob_sha=BLOB_SHA,
                start_line=41,
                end_line=42,
            )
        ],
        proposed_fix=ProposedFix(
            summary="Handle payment errors.",
            steps=["Catch expected payment failures."],
            affected_paths=[SOURCE_PATH],
        ),
        confidence=0.8,
    )
    try:
        with pytest.raises(OllamaLocalResponseError, match="not tied"):
            await provider.propose_patch(
                request,
                diagnosis,
                [PatchSourceFile(path=SOURCE_PATH, blob_sha="d" * 40, content=SOURCE_CONTENT)],
                patch_id="wrong-blob",
            )
    finally:
        await client.aclose()

    assert calls == []


def test_rejects_non_loopback_urls_and_unsafe_model_names() -> None:
    for unsafe_url in (
        "https://api.example.test",
        "http://10.0.0.5:11434",
        "http://localhost:11434/proxy",
        "http://user@127.0.0.1:11434",
    ):
        with pytest.raises(OllamaLocalConfigurationError):
            OllamaLocalProvider("qwen2.5-coder:7b", base_url=unsafe_url)

    with pytest.raises(OllamaLocalConfigurationError):
        OllamaLocalProvider("qwen coder; rm -rf /")

    with pytest.raises(OllamaLocalConfigurationError):
        OllamaLocalProvider("qwen2.5-coder:7b", timeout=httpx.Timeout(None))


@pytest.mark.asyncio
async def test_redirects_are_never_followed_outside_loopback() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://example.invalid/ollama"})

    provider, client = _provider(handler)
    try:
        with pytest.raises(OllamaLocalAPIError) as captured:
            await provider.diagnose(_request())
    finally:
        await client.aclose()

    assert captured.value.status_code == 302
    assert [str(call.url) for call in calls] == ["http://127.0.0.1:11434/api/chat"]


@pytest.mark.asyncio
async def test_oversized_or_incomplete_model_output_is_rejected_before_json_contract_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"message":{"role":"assistant","content":"' + b"x" * 200 + b'"},"done":true}',
        )

    provider, client = _provider(handler, limits=OllamaLocalLimits(max_response_bytes=64))
    try:
        with pytest.raises(OllamaLocalResponseError, match="output budget"):
            await provider.diagnose(_request())
    finally:
        await client.aclose()
