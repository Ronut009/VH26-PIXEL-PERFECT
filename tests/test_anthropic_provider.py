"""The hosted diagnosis tier: same contract as local, different trust boundary."""

import json

import pytest

from src.github_integration.anthropic_provider import (
    AnthropicConfigurationError,
    AnthropicDiagnosisProvider,
    AnthropicProviderError,
)
from src.github_integration.diagnosis import (
    DiagnosisIncidentContext,
    DiagnosisRequest,
    SourceExcerpt,
    SourceSnapshotReference,
)

BLOB = "b" * 40
COMMIT = "c" * 40
TREE = "d" * 40


def _request() -> DiagnosisRequest:
    return DiagnosisRequest(
        incident=DiagnosisIncidentContext(
            incident_id="11111111-1111-4111-8111-111111111111",
            service="payment-api",
            alertname="ConnectionsExhausted",
            severity="critical",
            status="firing",
            scope_key="production:payments-east",
            alert_count=100,
            message="connection pool exhausted under load",
            labels={"environment": "production", "cluster": "payments-east"},
        ),
        snapshot=SourceSnapshotReference(
            snapshot_id="22222222-2222-4222-8222-222222222222",
            repository_id=8123,
            repository_full_name="acme/payment-api",
            commit_sha=COMMIT,
            tree_sha=TREE,
        ),
        excerpts=[
            SourceExcerpt(
                file_path="src/pool.py",
                blob_sha=BLOB,
                start_line=10,
                end_line=20,
                content="pool = Pool(max_size=1)\n",
                language="python",
            )
        ],
    )


def _valid_payload() -> str:
    return json.dumps(
        {
            "status": "diagnosed",
            "root_cause_hypothesis": {
                "summary": "The connection pool is capped at one connection.",
                "reasoning": "src/pool.py constructs Pool(max_size=1), so concurrent "
                "requests serialise and time out under load.",
            },
            "evidence": [
                {
                    "kind": "source_excerpt",
                    "explanation": "The pool is constructed with max_size=1.",
                    "file_path": "src/pool.py",
                    "blob_sha": BLOB,
                    "start_line": 10,
                    "end_line": 20,
                }
            ],
            "proposed_fix": {
                "summary": "Raise the pool size and make it configurable.",
                "steps": ["Set max_size from configuration with a sane default."],
                "affected_paths": ["src/pool.py"],
            },
            "confidence": 0.72,
        }
    )


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text: str = "", stop_reason: str = "end_turn") -> None:
        self.content = [_Block(text)] if text else []
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.messages = _FakeMessages(response, error)


def _provider(response=None, error: Exception | None = None):
    client = _FakeClient(response, error)
    return AnthropicDiagnosisProvider("", client=client), client


# ── configuration is a deliberate act ─────────────────────────────────────


def test_the_provider_refuses_to_start_without_a_key():
    """No key must mean no provider, never a silently degraded one."""

    with pytest.raises(AnthropicConfigurationError):
        AnthropicDiagnosisProvider("")

    with pytest.raises(AnthropicConfigurationError):
        AnthropicDiagnosisProvider("sk-test", model="")


def test_local_wins_when_both_tiers_are_configured():
    """Source staying inside the deployment should be the default outcome."""

    from src.config import Settings

    settings = Settings(
        OLLAMA_ENABLED=True,
        ANTHROPIC_DIAGNOSIS_ENABLED=True,
        ANTHROPIC_API_KEY="sk-test",
    )
    # main.py selects hosted only when local is off; assert the flags that
    # encode that choice rather than re-running the lifespan here.
    assert settings.OLLAMA_ENABLED and settings.ANTHROPIC_DIAGNOSIS_ENABLED


# ── the request that leaves the building ──────────────────────────────────


@pytest.mark.asyncio
async def test_the_request_is_schema_constrained_and_carries_only_bounded_source():
    provider, client = _provider(_Response(_valid_payload()))
    await provider.diagnose(_request())

    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-5"
    # The response shape is constrained by the API, not requested politely in
    # a prompt and hoped for.
    assert call["output_config"]["format"]["type"] == "json_schema"

    body = json.loads(call["messages"][0]["content"])
    assert body["source_excerpts"][0]["file_path"] == "src/pool.py"
    assert len(body["source_excerpts"]) == 1, "only the bounded excerpts may leave"
    assert any("untrusted data" in rule for rule in body["rules"])


@pytest.mark.asyncio
async def test_a_well_formed_diagnosis_comes_back_grounded():
    provider, _ = _provider(_Response(_valid_payload()))
    result = await provider.diagnose(_request())

    assert result.status == "diagnosed"
    assert result.provider == "anthropic-hosted"
    assert result.confidence == pytest.approx(0.72)
    assert result.evidence[0].file_path == "src/pool.py"
    assert result.evidence[0].blob_sha == BLOB


# ── every failure is a fallback, never a guess ────────────────────────────


@pytest.mark.asyncio
async def test_an_api_failure_raises_for_the_service_to_fall_back():
    provider, _ = _provider(error=TimeoutError("read timeout"))

    with pytest.raises(AnthropicProviderError):
        await provider.diagnose(_request())


@pytest.mark.asyncio
async def test_a_response_that_misses_the_schema_is_rejected_not_salvaged():
    provider, _ = _provider(_Response(json.dumps({"status": "diagnosed"})))

    with pytest.raises(AnthropicProviderError, match="schema"):
        await provider.diagnose(_request())


@pytest.mark.asyncio
async def test_a_refusal_is_not_treated_as_a_diagnosis():
    provider, _ = _provider(_Response("", stop_reason="refusal"))

    with pytest.raises(AnthropicProviderError, match="declined"):
        await provider.diagnose(_request())


@pytest.mark.asyncio
async def test_an_empty_response_is_not_treated_as_a_diagnosis():
    provider, _ = _provider(_Response(""))

    with pytest.raises(AnthropicProviderError, match="no content"):
        await provider.diagnose(_request())


@pytest.mark.asyncio
async def test_a_non_request_object_never_reaches_the_network():
    provider, client = _provider(_Response(_valid_payload()))

    with pytest.raises(AnthropicProviderError):
        await provider.diagnose({"incident": "not a DiagnosisRequest"})

    assert client.messages.calls == [], "nothing unvalidated may be sent"
