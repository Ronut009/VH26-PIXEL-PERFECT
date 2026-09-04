"""Pure helpers joining bounded GitHub source with diagnosis and patch review.

The HTTP router owns authentication, GitHub reads, and persistence.  These
helpers deliberately own none of those concerns: they only transform already
bounded in-memory data into strict diagnosis inputs or a reviewable local patch
proposal.  Keeping the conversion here makes the Phase 2--4 safety invariants
easy to exercise without a network, a database, or a checkout.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import PurePosixPath
from typing import Mapping, Sequence

from src.github_integration.diagnosis import (
    DiagnosisIncidentContext,
    DiagnosisRequest,
    SourceExcerpt as DiagnosisSourceExcerpt,
    SourceSnapshotReference,
)
from src.github_integration.source_context import SourceContext
from src.github_integration.workspace import (
    ChangeAction,
    PatchValidationError,
    ProposedFileChange,
    ProposedPatch,
)


_LANGUAGES_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".md": "markdown",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def build_diagnosis_request(
    *,
    incident: DiagnosisIncidentContext,
    snapshot: SourceSnapshotReference,
    source_context: SourceContext,
) -> DiagnosisRequest:
    """Convert in-memory source excerpts into the stricter diagnosis contract.

    ``SourceContextPolicy`` should be configured no larger than the diagnosis
    byte limits.  We intentionally leave final validation to ``DiagnosisRequest``
    as a second line of defence; an invalid Git object ID or oversize excerpt
    cannot reach a provider.
    """

    excerpts: list[DiagnosisSourceExcerpt] = []
    for excerpt in source_context.excerpts:
        # A zero-byte source file is harmless but cannot ground a useful
        # diagnosis because the diagnosis contract requires visible text.
        if not excerpt.text.strip():
            continue
        excerpts.append(
            DiagnosisSourceExcerpt(
                file_path=excerpt.path,
                blob_sha=excerpt.blob_sha,
                start_line=1,
                end_line=excerpt.text.count("\n") + 1,
                content=excerpt.text,
                language=language_for_path(excerpt.path),
            )
        )
    return DiagnosisRequest(incident=incident, snapshot=snapshot, excerpts=excerpts)


def language_for_path(path: str) -> str | None:
    """Return a compact language hint without trusting a filesystem path."""

    return _LANGUAGES_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())


def bind_patch_to_snapshot(
    proposal: ProposedPatch,
    *,
    base_files: Mapping[str, str],
    allowed_paths: Sequence[str] | None = None,
) -> ProposedPatch:
    """Restrict a patch preview to supplied immutable source files.

    The generated diff is grounded in files fetched from the exact snapshot
    used for diagnosis; it cannot create a surprise file or target a path that
    was not supplied to the model. Updates and deletions remain available for
    legitimate corrective changes, but creation is intentionally held back
    until the product has a reviewed test-file policy. The helper attaches the
    exact SHA-256 of each source string rather than relying on a language model
    to calculate a digest.
    """

    supplied_paths = set(base_files)
    permitted_paths = set(allowed_paths) if allowed_paths is not None else supplied_paths
    if not permitted_paths:
        raise PatchValidationError("no pinned source files are available for patch preview")

    changes: list[ProposedFileChange] = []
    for change in proposal.changes:
        try:
            action = ChangeAction(change.action)
        except (TypeError, ValueError) as exc:
            raise PatchValidationError("patch change action is invalid") from exc
        if action not in {ChangeAction.UPDATE, ChangeAction.DELETE}:
            raise PatchValidationError(
                "Phase 4 patch preview permits updates or deletions of supplied snapshot files only"
            )
        if change.path not in supplied_paths or change.path not in permitted_paths:
            raise PatchValidationError(
                "patch change must target a source file supplied from the pinned snapshot"
            )
        source_digest = hashlib.sha256(base_files[change.path].encode("utf-8")).hexdigest()
        if change.expected_sha256 is not None and change.expected_sha256.lower() != source_digest:
            raise PatchValidationError(
                "patch expected_sha256 does not match the pinned source snapshot"
            )
        changes.append(replace(change, action=action, expected_sha256=source_digest))

    return replace(proposal, changes=tuple(changes))


def patch_review_payload(review: object) -> dict[str, object]:
    """Return a JSON-ready, explicit projection of a local patch review."""

    # Import lazily to avoid making the narrow conversion helpers depend on
    # workspace implementation details at module import time.
    from src.github_integration.workspace import PatchReview

    if not isinstance(review, PatchReview):
        raise TypeError("review must be a PatchReview")
    return {
        "patch_id": review.patch_id,
        "summary": review.summary,
        "rationale": review.rationale,
        "changed_files": [
            {
                "path": item.path,
                "action": item.action.value,
                "before_sha256": item.before_sha256,
                "after_sha256": item.after_sha256,
                "before_bytes": item.before_bytes,
                "after_bytes": item.after_bytes,
                "explanation": item.explanation,
            }
            for item in review.changed_files
        ],
        "unified_diff": review.unified_diff,
        "metadata": dict(review.metadata),
    }


__all__ = [
    "bind_patch_to_snapshot",
    "build_diagnosis_request",
    "language_for_path",
    "patch_review_payload",
]
