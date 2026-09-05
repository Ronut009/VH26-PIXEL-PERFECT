"""Safe, provider-agnostic diagnosis contracts for GitHub-backed incidents.

This module deliberately has no model SDK, HTTP client, persistence layer, or
GitHub write capability.  It is the boundary between a bounded, immutable
source snapshot and a future diagnosis provider.  Providers receive only the
incident context and excerpts selected by the caller; they cannot ask this
module to fetch more source or modify a repository.

``DiagnosisService`` also fails closed.  A provider result must cite supplied
source excerpts and propose only human-reviewed changes to supplied paths.
Anything unavailable, malformed, or ungrounded becomes a transparent safe
fallback instead of an overconfident diagnosis.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from src.utils.logging import get_logger
from uuid import UUID
import re

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


# These limits constrain the source material handed to a provider in one
# diagnosis request.  They are byte-based so non-ASCII source cannot bypass the
# budget by using fewer Python characters.
MAX_SOURCE_EXCERPTS = 20
MAX_EXCERPT_BYTES = 8 * 1024
MAX_TOTAL_EXCERPT_BYTES = 48 * 1024
MAX_INCIDENT_MESSAGE_CHARS = 8 * 1024
MAX_LABELS = 64

_GIT_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


logger = get_logger(__name__)


class DiagnosisContractError(ValueError):
    """Raised when a provider returns an unsafe or ungrounded diagnosis."""


def _required_text(value: str, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return normalized


def _optional_text(value: str | None, field_name: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name, maximum=maximum)


def _git_object_id(value: str, field_name: str) -> str:
    normalized = _required_text(value, field_name, maximum=64)
    if not _GIT_OBJECT_ID.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a 40- or 64-character Git object ID")
    return normalized.lower()


def _repository_path(value: str, field_name: str = "file_path") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not value or len(value) > 1_024:
        raise ValueError(f"{field_name} must be between 1 and 1024 characters")
    if "\x00" in value or "\\" in value or value.startswith("/"):
        raise ValueError(f"{field_name} must be a relative POSIX repository path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} must not contain empty, '.' or '..' path segments")
    return value


def _provider_label(value: str) -> str:
    normalized = _required_text(value, "provider", maximum=64).lower()
    if not _PROVIDER_NAME.fullmatch(normalized):
        raise ValueError("provider must contain only lowercase letters, digits, '.', '_' or '-'")
    return normalized


class DiagnosisIncidentContext(BaseModel):
    """A bounded, normalized representation of the incident being diagnosed."""

    model_config = ConfigDict(extra="forbid")

    incident_id: UUID
    service: str = Field(max_length=256)
    alertname: str = Field(max_length=512)
    severity: str = Field(max_length=64)
    status: Literal["firing", "resolved"]
    scope_key: str = Field(max_length=512)
    alert_count: int = Field(ge=1, le=1_000_000)
    message: str = Field(max_length=MAX_INCIDENT_MESSAGE_CHARS)
    labels: dict[str, str] = Field(default_factory=dict)
    summary: str | None = Field(default=None, max_length=MAX_INCIDENT_MESSAGE_CHARS)
    graph_root_cause_hint: str | None = Field(default=None, max_length=2_000)

    @field_validator("service", "alertname", "severity", "scope_key", "message")
    @classmethod
    def _validate_required_text(cls, value: str, info) -> str:
        return _required_text(value, info.field_name, maximum=MAX_INCIDENT_MESSAGE_CHARS)

    @field_validator("summary", "graph_root_cause_hint")
    @classmethod
    def _validate_optional_text(cls, value: str | None, info) -> str | None:
        return _optional_text(value, info.field_name, maximum=MAX_INCIDENT_MESSAGE_CHARS)

    @field_validator("labels")
    @classmethod
    def _validate_labels(cls, labels: dict[str, str]) -> dict[str, str]:
        if len(labels) > MAX_LABELS:
            raise ValueError(f"labels must contain at most {MAX_LABELS} entries")
        normalized: dict[str, str] = {}
        for key, value in labels.items():
            clean_key = _required_text(key, "label key", maximum=128)
            clean_value = _required_text(value, f"label '{clean_key}'", maximum=1_024)
            normalized[clean_key] = clean_value
        return normalized


class SourceSnapshotReference(BaseModel):
    """Identity of the immutable GitHub snapshot from which excerpts came."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    repository_id: int = Field(gt=0)
    repository_full_name: str = Field(max_length=256)
    commit_sha: str
    tree_sha: str

    @field_validator("repository_full_name")
    @classmethod
    def _validate_repository_full_name(cls, value: str) -> str:
        normalized = _required_text(value, "repository_full_name", maximum=256)
        parts = normalized.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repository_full_name must be an owner/repository pair")
        return normalized

    @field_validator("commit_sha", "tree_sha")
    @classmethod
    def _validate_object_id(cls, value: str, info) -> str:
        return _git_object_id(value, info.field_name)


class SourceExcerpt(BaseModel):
    """A bounded source fragment tied to one immutable Git blob."""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    blob_sha: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content: str = Field(min_length=1)
    language: str | None = Field(default=None, max_length=64)

    @field_validator("file_path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _repository_path(value)

    @field_validator("blob_sha")
    @classmethod
    def _validate_blob_sha(cls, value: str) -> str:
        return _git_object_id(value, "blob_sha")

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("content must not contain NUL bytes")
        if not value.strip():
            raise ValueError("content must not be blank")
        if len(value.encode("utf-8")) > MAX_EXCERPT_BYTES:
            raise ValueError(f"content must be at most {MAX_EXCERPT_BYTES} UTF-8 bytes")
        return value

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: str | None) -> str | None:
        return _optional_text(value, "language", maximum=64)

    @model_validator(mode="after")
    def _validate_line_range(self) -> "SourceExcerpt":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self

    @property
    def byte_count(self) -> int:
        """The exact input-budget cost of this excerpt."""

        return len(self.content.encode("utf-8"))


class DiagnosisRequest(BaseModel):
    """The full, bounded input passed to a diagnosis provider."""

    model_config = ConfigDict(extra="forbid")

    incident: DiagnosisIncidentContext
    snapshot: SourceSnapshotReference
    excerpts: list[SourceExcerpt] = Field(default_factory=list, max_length=MAX_SOURCE_EXCERPTS)

    @model_validator(mode="after")
    def _validate_excerpts_budget(self) -> "DiagnosisRequest":
        total_bytes = sum(excerpt.byte_count for excerpt in self.excerpts)
        if total_bytes > MAX_TOTAL_EXCERPT_BYTES:
            raise ValueError(
                f"source excerpts must total at most {MAX_TOTAL_EXCERPT_BYTES} UTF-8 bytes"
            )
        return self

    @property
    def source_bytes(self) -> int:
        """Return the total excerpt bytes sent to a provider."""

        return sum(excerpt.byte_count for excerpt in self.excerpts)


class DiagnosisEvidence(BaseModel):
    """A concise explanation tied either to the incident or a source excerpt."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["incident", "source_excerpt"]
    explanation: str = Field(max_length=2_000)
    file_path: str | None = None
    blob_sha: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @field_validator("explanation")
    @classmethod
    def _validate_explanation(cls, value: str) -> str:
        return _required_text(value, "explanation", maximum=2_000)

    @field_validator("file_path")
    @classmethod
    def _validate_evidence_path(cls, value: str | None) -> str | None:
        return None if value is None else _repository_path(value)

    @field_validator("blob_sha")
    @classmethod
    def _validate_evidence_blob_sha(cls, value: str | None) -> str | None:
        return None if value is None else _git_object_id(value, "blob_sha")

    @model_validator(mode="after")
    def _validate_evidence_location(self) -> "DiagnosisEvidence":
        has_any_location = any(
            value is not None
            for value in (self.file_path, self.blob_sha, self.start_line, self.end_line)
        )
        if self.kind == "incident":
            if has_any_location:
                raise ValueError("incident evidence must not include source coordinates")
            return self
        if None in (self.file_path, self.blob_sha, self.start_line, self.end_line):
            raise ValueError("source_excerpt evidence requires a complete source location")
        assert self.start_line is not None and self.end_line is not None
        if self.end_line < self.start_line:
            raise ValueError("evidence end_line must be greater than or equal to start_line")
        return self


class RootCauseHypothesis(BaseModel):
    """A clear, reviewable hypothesis rather than an asserted fact."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=2_000)
    reasoning: str = Field(max_length=4_000)

    @field_validator("summary", "reasoning")
    @classmethod
    def _validate_text(cls, value: str, info) -> str:
        return _required_text(value, info.field_name, maximum=4_000)


class ProposedFix(BaseModel):
    """A human-reviewed change recommendation, never an automatic mutation."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=2_000)
    steps: list[str] = Field(min_length=1, max_length=10)
    affected_paths: list[str] = Field(default_factory=list, max_length=20)
    requires_human_review: Literal[True] = True
    automatically_applied: Literal[False] = False

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        return _required_text(value, "summary", maximum=2_000)

    @field_validator("steps")
    @classmethod
    def _validate_steps(cls, values: list[str]) -> list[str]:
        return [_required_text(value, "fix step", maximum=1_000) for value in values]

    @field_validator("affected_paths")
    @classmethod
    def _validate_affected_paths(cls, values: list[str]) -> list[str]:
        return [_repository_path(value, "affected_paths item") for value in values]


FallbackReason = Literal[
    "no_source_excerpts",
    "provider_unavailable",
    "invalid_provider_result",
    "insufficient_evidence",
]


class SafeFallback(BaseModel):
    """Actionable response returned when a grounded diagnosis is unavailable."""

    model_config = ConfigDict(extra="forbid")

    reason: FallbackReason
    message: str = Field(max_length=2_000)
    next_steps: list[str] = Field(min_length=1, max_length=8)
    requires_human_review: Literal[True] = True

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        return _required_text(value, "message", maximum=2_000)

    @field_validator("next_steps")
    @classmethod
    def _validate_next_steps(cls, values: list[str]) -> list[str]:
        return [_required_text(value, "fallback next step", maximum=1_000) for value in values]


class DiagnosisResult(BaseModel):
    """An explainable diagnosis or a safe, non-speculative fallback."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["diagnosed", "fallback"]
    provider: str = Field(max_length=64)
    root_cause_hypothesis: RootCauseHypothesis | None = None
    evidence: list[DiagnosisEvidence] = Field(default_factory=list, max_length=24)
    proposed_fix: ProposedFix | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    fallback: SafeFallback | None = None

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        return _provider_label(value)

    @model_validator(mode="after")
    def _validate_shape(self) -> "DiagnosisResult":
        if self.status == "diagnosed":
            if self.root_cause_hypothesis is None:
                raise ValueError("a diagnosed result requires root_cause_hypothesis")
            if not self.evidence:
                raise ValueError("a diagnosed result requires evidence")
            if self.proposed_fix is None:
                raise ValueError("a diagnosed result requires proposed_fix")
            if self.fallback is not None:
                raise ValueError("a diagnosed result must not include fallback")
            return self

        if any(
            value is not None
            for value in (self.root_cause_hypothesis, self.proposed_fix)
        ) or self.evidence:
            raise ValueError("a fallback result must not include an ungrounded diagnosis")
        if self.fallback is None:
            raise ValueError("a fallback result requires fallback")
        if self.confidence != 0.0:
            raise ValueError("a fallback result must use zero confidence")
        return self


@runtime_checkable
class DiagnosisProvider(Protocol):
    """Provider-neutral async interface for a future model implementation."""

    name: str

    async def diagnose(self, request: DiagnosisRequest) -> DiagnosisResult:
        """Return a diagnosis without writing to GitHub or another provider."""


def safe_fallback(reason: FallbackReason) -> DiagnosisResult:
    """Build a generic, non-speculative response without exposing provider errors."""

    messages: dict[FallbackReason, tuple[str, list[str]]] = {
        "no_source_excerpts": (
            "No bounded source excerpts are available for this incident, so PulseGraph cannot ground a code diagnosis.",
            [
                "Create or select a pinned repository snapshot and bounded relevant excerpts.",
                "Review the incident timeline and logs before proposing a code change.",
            ],
        ),
        "provider_unavailable": (
            "A diagnosis provider is unavailable, so PulseGraph is withholding an unverified root-cause claim.",
            [
                "Retry diagnosis after the provider is healthy.",
                "Review the pinned source excerpts and incident evidence manually.",
            ],
        ),
        "invalid_provider_result": (
            "The diagnosis result was not grounded in the supplied snapshot, so PulseGraph did not surface it.",
            [
                "Retry with a provider that cites only the supplied excerpts.",
                "Ask an engineer to review the incident and pinned snapshot manually.",
            ],
        ),
        "insufficient_evidence": (
            "The supplied evidence is insufficient for a safe root-cause hypothesis.",
            [
                "Add bounded excerpts that cover the failing code path.",
                "Review logs, metrics, and the pinned snapshot with an engineer.",
            ],
        ),
    }
    message, next_steps = messages[reason]
    return DiagnosisResult(
        status="fallback",
        provider="safe-fallback",
        confidence=0.0,
        fallback=SafeFallback(reason=reason, message=message, next_steps=next_steps),
    )


class SafeFallbackDiagnosisProvider:
    """Explicit offline provider useful when no model provider is configured."""

    name = "safe-fallback"

    async def diagnose(self, request: DiagnosisRequest) -> DiagnosisResult:
        return safe_fallback(
            "no_source_excerpts" if not request.excerpts else "insufficient_evidence"
        )


class DiagnosisService:
    """Try each provider in order and enforce grounding and review invariants.

    Providers are attempted in the order given and the first grounded answer
    wins, so the caller sets the policy by ordering them. The intended order is
    local first: it keeps source inside the deployment, and only a local model
    that could not produce a usable answer should cause any code to leave.

    A provider is skipped for either kind of failure - it raised, or it answered
    and the answer failed grounding. The second case is the one that matters in
    practice: a small local model routinely returns well-formed JSON with an
    invented line range, which is a wrong answer rather than an outage, and
    before this it ended the attempt for everybody.
    """

    def __init__(self, *providers: DiagnosisProvider | None) -> None:
        self._providers = tuple(provider for provider in providers if provider is not None)
        self._provider_names = tuple(
            _provider_label(provider.name) for provider in self._providers
        )

    async def diagnose(self, request: DiagnosisRequest) -> DiagnosisResult:
        """Return a grounded diagnosis or a deliberately safe fallback.

        Provider exceptions are intentionally not included in a user-facing
        response: they can contain vendor internals or accidental prompt/source
        fragments.  Task cancellation is not swallowed because
        ``asyncio.CancelledError`` does not inherit from ``Exception``.
        """

        if not request.excerpts:
            return safe_fallback("no_source_excerpts")
        if not self._providers:
            return safe_fallback("provider_unavailable")

        # Carried so the caller learns why the *last* attempt failed, rather
        # than always seeing the first provider's reason.
        reason = "provider_unavailable"
        for provider, provider_name in zip(self._providers, self._provider_names):
            try:
                result = await provider.diagnose(request)
            except Exception as exc:
                # The response stays deliberately opaque - a provider exception
                # can carry vendor internals or fragments of the prompt and
                # source. But discarding it entirely left operators with
                # "provider_unavailable" and no way to tell a stopped Ollama
                # from a model that answered with unusable JSON. Log it; do not
                # return it.
                logger.warning(
                    "diagnosis_provider_failed",
                    provider=provider_name,
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                )
                reason = "provider_unavailable"
                continue

            if not isinstance(result, DiagnosisResult):
                reason = "invalid_provider_result"
                continue
            try:
                # ``model_copy`` and ``model_construct`` can bypass Pydantic's
                # construction validators.  Re-validating the public shape here
                # prevents a provider from turning such an object into a result
                # that the rest of PulseGraph trusts.
                validated_result = DiagnosisResult.model_validate(result.model_dump())
                self._validate_grounding(request, validated_result)
            except (DiagnosisContractError, ValidationError, TypeError, ValueError) as exc:
                # Distinct from the above: the provider answered, and the answer
                # was rejected. Usually an ungrounded citation - a file path or
                # line range the model invented rather than copied.
                logger.warning(
                    "diagnosis_result_rejected",
                    provider=provider_name,
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                )
                reason = "invalid_provider_result"
                continue

            # The service, not an untrusted provider response, identifies the
            # provider shown to callers - so the card names the model that
            # actually answered when a fallback provider took over.
            return validated_result.model_copy(update={"provider": provider_name})

        return safe_fallback(reason)

    @staticmethod
    def _validate_grounding(request: DiagnosisRequest, result: DiagnosisResult) -> None:
        if result.status == "fallback":
            return
        if result.status != "diagnosed":  # Defensive: Pydantic already constrains this.
            raise DiagnosisContractError("diagnosis status is unsupported")

        source_evidence = [evidence for evidence in result.evidence if evidence.kind == "source_excerpt"]
        if not source_evidence:
            raise DiagnosisContractError("diagnosed result must cite a source excerpt")

        for evidence in source_evidence:
            if not any(_evidence_within_excerpt(evidence, excerpt) for excerpt in request.excerpts):
                raise DiagnosisContractError("source evidence does not belong to a supplied excerpt")

        assert result.proposed_fix is not None
        excerpt_paths = {excerpt.file_path for excerpt in request.excerpts}
        if any(path not in excerpt_paths for path in result.proposed_fix.affected_paths):
            raise DiagnosisContractError("proposed fix references a path outside supplied excerpts")


def _evidence_within_excerpt(evidence: DiagnosisEvidence, excerpt: SourceExcerpt) -> bool:
    """Whether source evidence points to a line range supplied to the provider."""

    assert evidence.file_path is not None
    assert evidence.blob_sha is not None
    assert evidence.start_line is not None
    assert evidence.end_line is not None
    return (
        evidence.file_path == excerpt.file_path
        and evidence.blob_sha == excerpt.blob_sha
        and excerpt.start_line <= evidence.start_line <= evidence.end_line <= excerpt.end_line
    )


__all__ = [
    "DiagnosisContractError",
    "DiagnosisEvidence",
    "DiagnosisIncidentContext",
    "DiagnosisProvider",
    "DiagnosisRequest",
    "DiagnosisResult",
    "DiagnosisService",
    "FallbackReason",
    "MAX_EXCERPT_BYTES",
    "MAX_SOURCE_EXCERPTS",
    "MAX_TOTAL_EXCERPT_BYTES",
    "ProposedFix",
    "RootCauseHypothesis",
    "SafeFallback",
    "SafeFallbackDiagnosisProvider",
    "SourceExcerpt",
    "SourceSnapshotReference",
    "safe_fallback",
]
