"""Focused tests for the provider-neutral, safe diagnosis boundary."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from src.github_integration.diagnosis import (
    DiagnosisEvidence,
    DiagnosisIncidentContext,
    DiagnosisRequest,
    DiagnosisResult,
    DiagnosisService,
    MAX_EXCERPT_BYTES,
    ProposedFix,
    RootCauseHypothesis,
    SafeFallbackDiagnosisProvider,
    SourceExcerpt,
    SourceSnapshotReference,
)


INCIDENT_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40
BLOB_SHA = "c" * 40


def _incident() -> DiagnosisIncidentContext:
    return DiagnosisIncidentContext(
        incident_id=INCIDENT_ID,
        service="checkout-api",
        alertname="CheckoutErrorRateHigh",
        severity="critical",
        status="firing",
        scope_key="production:payments-east",
        alert_count=100,
        message="checkout 5xx rate rose after the deployment",
        labels={"environment": "production", "cluster": "payments-east"},
        graph_root_cause_hint="root_cause=11111111-1111-1111-1111-111111111111",
    )


def _snapshot() -> SourceSnapshotReference:
    return SourceSnapshotReference(
        snapshot_id=SNAPSHOT_ID,
        repository_id=8123,
        repository_full_name="acme/checkout-api",
        commit_sha=COMMIT_SHA,
        tree_sha=TREE_SHA,
    )


def _excerpt(
    *,
    file_path: str = "src/handlers/checkout.py",
    blob_sha: str = BLOB_SHA,
    start_line: int = 40,
    end_line: int = 48,
    content: str = "def handle_checkout(request):\n    return charge(request)\n",
) -> SourceExcerpt:
    return SourceExcerpt(
        file_path=file_path,
        blob_sha=blob_sha,
        start_line=start_line,
        end_line=end_line,
        content=content,
        language="python",
    )


def _request(*, excerpts: list[SourceExcerpt] | None = None) -> DiagnosisRequest:
    return DiagnosisRequest(
        incident=_incident(),
        snapshot=_snapshot(),
        excerpts=[_excerpt()] if excerpts is None else excerpts,
    )


def _grounded_result(request: DiagnosisRequest) -> DiagnosisResult:
    excerpt = request.excerpts[0]
    return DiagnosisResult(
        status="diagnosed",
        provider="untrusted-response-label",
        root_cause_hypothesis=RootCauseHypothesis(
            summary="The checkout handler lets charge failures escape as 5xx responses.",
            reasoning="The incident message reports checkout 5xx errors, and the selected handler calls charge without a failure boundary.",
        ),
        evidence=[
            DiagnosisEvidence(
                kind="incident",
                explanation="The coalesced incident contains 100 checkout 5xx alerts in production.",
            ),
            DiagnosisEvidence(
                kind="source_excerpt",
                explanation="The charge call in the supplied handler has no local error handling.",
                file_path=excerpt.file_path,
                blob_sha=excerpt.blob_sha,
                start_line=41,
                end_line=42,
            ),
        ],
        proposed_fix=ProposedFix(
            summary="Handle the known payment error at the checkout boundary and return the intended response.",
            steps=[
                "Catch the expected payment failure around the charge call.",
                "Add a regression test for the failure response before merging.",
            ],
            affected_paths=[excerpt.file_path],
        ),
        confidence=0.78,
    )


class GroundedProvider:
    name = "local-review-provider"

    async def diagnose(self, request: DiagnosisRequest) -> DiagnosisResult:
        return _grounded_result(request)


@pytest.mark.asyncio
async def test_service_returns_a_grounded_explainable_diagnosis() -> None:
    request = _request()

    result = await DiagnosisService(GroundedProvider()).diagnose(request)

    assert result.status == "diagnosed"
    # The service owns the displayed provider identity; a response cannot
    # spoof it with the provider field it returns.
    assert result.provider == "local-review-provider"
    assert result.root_cause_hypothesis is not None
    assert "checkout handler" in result.root_cause_hypothesis.summary
    assert result.confidence == 0.78
    assert result.proposed_fix is not None
    assert result.proposed_fix.requires_human_review is True
    assert result.proposed_fix.automatically_applied is False
    assert result.proposed_fix.affected_paths == ["src/handlers/checkout.py"]
    assert result.fallback is None


@pytest.mark.asyncio
async def test_missing_source_excerpts_never_calls_provider_and_returns_safe_fallback() -> None:
    class ShouldNotRunProvider:
        name = "should-not-run"

        async def diagnose(self, request: DiagnosisRequest) -> DiagnosisResult:
            raise AssertionError("a source-grounded diagnosis must not run without source")

    result = await DiagnosisService(ShouldNotRunProvider()).diagnose(_request(excerpts=[]))

    assert result.status == "fallback"
    assert result.confidence == 0.0
    assert result.provider == "safe-fallback"
    assert result.fallback is not None
    assert result.fallback.reason == "no_source_excerpts"
    assert result.evidence == []
    assert result.proposed_fix is None


@pytest.mark.asyncio
async def test_provider_exception_is_not_exposed_and_uses_safe_fallback() -> None:
    class BrokenProvider:
        name = "broken-provider"

        async def diagnose(self, request: DiagnosisRequest) -> DiagnosisResult:
            raise RuntimeError("provider-secret=do-not-leak")

    result = await DiagnosisService(BrokenProvider()).diagnose(_request())

    assert result.status == "fallback"
    assert result.fallback is not None
    assert result.fallback.reason == "provider_unavailable"
    assert "provider-secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_unconfigured_provider_uses_safe_fallback() -> None:
    result = await DiagnosisService().diagnose(_request())

    assert result.status == "fallback"
    assert result.fallback is not None
    assert result.fallback.reason == "provider_unavailable"
    assert result.provider == "safe-fallback"


@pytest.mark.asyncio
async def test_ungrounded_evidence_or_affected_path_is_rejected_to_safe_fallback() -> None:
    class HallucinatingProvider:
        name = "hallucinating-provider"

        async def diagnose(self, request: DiagnosisRequest) -> DiagnosisResult:
            result = _grounded_result(request)
            return result.model_copy(
                update={
                    "evidence": [
                        DiagnosisEvidence(
                            kind="source_excerpt",
                            explanation="This claimed line was never supplied.",
                            file_path="src/never-supplied.py",
                            blob_sha="d" * 40,
                            start_line=1,
                            end_line=1,
                        )
                    ],
                    "proposed_fix": ProposedFix(
                        summary="Change an uninspected file.",
                        steps=["Make an ungrounded edit."],
                        affected_paths=["src/never-supplied.py"],
                    ),
                }
            )

    result = await DiagnosisService(HallucinatingProvider()).diagnose(_request())

    assert result.status == "fallback"
    assert result.fallback is not None
    assert result.fallback.reason == "invalid_provider_result"


@pytest.mark.asyncio
async def test_explicit_offline_provider_uses_safe_insufficient_evidence_fallback() -> None:
    result = await DiagnosisService(SafeFallbackDiagnosisProvider()).diagnose(_request())

    assert result.status == "fallback"
    assert result.fallback is not None
    assert result.fallback.reason == "insufficient_evidence"
    assert result.provider == "safe-fallback"


def test_source_excerpts_enforce_immutable_identifiers_safe_paths_and_byte_budgets() -> None:
    with pytest.raises(ValidationError, match="Git object ID"):
        _excerpt(blob_sha="not-a-git-object")

    with pytest.raises(ValidationError, match="relative POSIX repository path"):
        _excerpt(file_path="C:\\source\\checkout.py")

    with pytest.raises(ValidationError, match="must not contain empty, '.' or '..'"):
        _excerpt(file_path="src/../secrets.py")

    with pytest.raises(ValidationError, match="end_line"):
        _excerpt(start_line=20, end_line=19)

    with pytest.raises(ValidationError, match="UTF-8 bytes"):
        _excerpt(content="x" * (MAX_EXCERPT_BYTES + 1))


def test_request_enforces_the_total_bounded_source_budget() -> None:
    excerpts = [
        _excerpt(
            file_path=f"src/module_{index}.py",
            blob_sha=f"{index:x}" * 40,
            content="x" * MAX_EXCERPT_BYTES,
        )
        for index in range(1, 8)
    ]

    with pytest.raises(ValidationError, match="source excerpts must total"):
        _request(excerpts=excerpts)


def test_result_schema_forbids_auto_apply_and_unguarded_fallbacks() -> None:
    with pytest.raises(ValidationError, match="automatically_applied"):
        ProposedFix(
            summary="Unsafe change",
            steps=["Apply immediately"],
            automatically_applied=True,
        )

    with pytest.raises(ValidationError, match="fallback result must use zero confidence"):
        DiagnosisResult(
            status="fallback",
            provider="safe-fallback",
            confidence=0.1,
            fallback={
                "reason": "insufficient_evidence",
                "message": "Need more evidence.",
                "next_steps": ["Inspect source."],
            },
        )


# ── provider chain ───────────────────────────────────────────────────────────
# Local first, hosted second. The ordering is the privacy policy: source only
# leaves the deployment when the local model could not produce a usable answer.


class _RaisingProvider:
    name = "local-that-is-down"

    def __init__(self) -> None:
        self.calls = 0

    async def diagnose(self, request: DiagnosisRequest) -> DiagnosisResult:
        self.calls += 1
        raise RuntimeError("ollama-secret=do-not-leak")


class _UngroundedProvider:
    """Answers, well-formed, citing a line range it was never given.

    This is the common local-model failure: not an outage, a wrong answer. It
    used to end the attempt for every provider behind it.
    """

    name = "local-that-hallucinates"

    def __init__(self) -> None:
        self.calls = 0

    async def diagnose(self, request: DiagnosisRequest) -> DiagnosisResult:
        self.calls += 1
        grounded = _grounded_result(request)
        invented = grounded.evidence[-1].model_copy(
            update={"start_line": 9_000, "end_line": 9_001}
        )
        return grounded.model_copy(update={"evidence": [grounded.evidence[0], invented]})


class _CountingGroundedProvider:
    name = "hosted-fallback"

    def __init__(self) -> None:
        self.calls = 0

    async def diagnose(self, request: DiagnosisRequest) -> DiagnosisResult:
        self.calls += 1
        return _grounded_result(request)


@pytest.mark.asyncio
async def test_a_hosted_provider_answers_when_the_local_one_is_down() -> None:
    local, hosted = _RaisingProvider(), _CountingGroundedProvider()

    result = await DiagnosisService(local, hosted).diagnose(_request())

    assert result.status == "diagnosed"
    assert result.provider == "hosted-fallback", "the card must name who answered"
    assert (local.calls, hosted.calls) == (1, 1)
    assert "ollama-secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_a_hosted_provider_answers_when_the_local_one_is_ungrounded() -> None:
    """The case that motivated the chain: answered, but not usably."""

    local, hosted = _UngroundedProvider(), _CountingGroundedProvider()

    result = await DiagnosisService(local, hosted).diagnose(_request())

    assert result.status == "diagnosed"
    assert result.provider == "hosted-fallback"
    assert (local.calls, hosted.calls) == (1, 1)


@pytest.mark.asyncio
async def test_a_working_local_provider_is_never_escalated() -> None:
    """No source leaves the deployment when the local model succeeds."""

    local, hosted = _CountingGroundedProvider(), _CountingGroundedProvider()
    local.name = "local-that-works"

    result = await DiagnosisService(local, hosted).diagnose(_request())

    assert result.provider == "local-that-works"
    assert hosted.calls == 0, "the hosted provider must not be called at all"


@pytest.mark.asyncio
async def test_every_provider_failing_reports_the_last_reason() -> None:
    local, hosted = _RaisingProvider(), _UngroundedProvider()

    result = await DiagnosisService(local, hosted).diagnose(_request())

    assert result.status == "fallback"
    assert result.fallback is not None
    # Not "provider_unavailable" from the first attempt: the last attempt
    # answered and was rejected, and that is the more useful thing to report.
    assert result.fallback.reason == "invalid_provider_result"
    assert result.provider == "safe-fallback"


@pytest.mark.asyncio
async def test_a_none_provider_in_the_chain_is_skipped() -> None:
    """Startup passes both slots, either of which may be unconfigured."""

    hosted = _CountingGroundedProvider()

    result = await DiagnosisService(None, hosted).diagnose(_request())

    assert result.status == "diagnosed"
    assert result.provider == "hosted-fallback"
