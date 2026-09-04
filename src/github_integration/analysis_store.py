"""Append-only, source-safe persistence for GitHub diagnosis results.

The GitHub snapshot tables intentionally contain only Git object metadata.  This
module keeps that boundary intact: it persists a small, whitelisted projection
of a diagnosis result and never serializes ``DiagnosisRequest.excerpts``, blob
contents, GitHub credentials, patches, or raw webhook/event payloads.

All functions operate on a caller-owned SQLite transaction/connection.  They
do not perform network I/O and do not alter the existing Phase 1 store API.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any
from uuid import UUID, uuid4

import aiosqlite
from pydantic import ValidationError

from src.github_integration.diagnosis import (
    DiagnosisIncidentContext,
    DiagnosisRequest,
    DiagnosisResult,
    SourceSnapshotReference,
)


class GitHubAnalysisStoreError(ValueError):
    """Raised when an analysis record cannot be safely loaded or persisted."""


class GitHubAnalysisBindingError(GitHubAnalysisStoreError):
    """Raised when an incident has no usable mapped repository/snapshot."""


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_PEM_BLOCK = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)
_GITHUB_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
_BEARER_TOKEN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r'\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*[\'\"]?[^\s,;\]\)\'\"]{6,}',
    re.IGNORECASE,
)
_SOURCE_LIKE_LINE = re.compile(
    r"^\s*(?:"
    r"async\s+def\b|def\b|class\b|function\b|import\b|from\b|return\b|"
    r"const\b|let\b|var\b|package\b|#include\b|"
    r"[A-Za-z_][A-Za-z0-9_.]*\s*=\s*[^=]"
    r")"
)


@dataclass(frozen=True, slots=True)
class ActiveServiceSnapshot:
    """An active service mapping pinned to a persisted immutable snapshot."""

    service: str
    repository_id: int
    installation_id: int
    owner: str
    repository: str
    full_name: str
    default_branch: str
    state_revision: int
    snapshot: SourceSnapshotReference


def _uuid_text(value: str | UUID, field_name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise GitHubAnalysisStoreError(f"{field_name} must be a UUID") from exc


def _service(value: str) -> str:
    if not isinstance(value, str):
        raise GitHubAnalysisStoreError("service must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 256 or "/" in normalized or "," in normalized:
        raise GitHubAnalysisStoreError("service is invalid")
    return normalized


def _json_object(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, str):
        raise GitHubAnalysisStoreError(f"{field_name} must be JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GitHubAnalysisStoreError(f"{field_name} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise GitHubAnalysisStoreError(f"{field_name} must contain an object")
    normalized: dict[str, str] = {}
    for key, item in parsed.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise GitHubAnalysisStoreError(f"{field_name} must contain string labels")
        normalized[key] = item
    return normalized


def _nullable_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GitHubAnalysisStoreError("stored text field is invalid")
    return value if value.strip() else None


def _snapshot_from_row(row: aiosqlite.Row | dict[str, Any]) -> SourceSnapshotReference:
    try:
        return SourceSnapshotReference(
            snapshot_id=row["snapshot_id"],
            repository_id=row["repository_id"],
            repository_full_name=row["full_name"],
            commit_sha=row["commit_sha"],
            tree_sha=row["tree_sha"],
        )
    except (KeyError, ValidationError, TypeError, ValueError) as exc:
        raise GitHubAnalysisStoreError("stored GitHub snapshot is invalid") from exc


def _active_snapshot_from_row(row: aiosqlite.Row | dict[str, Any]) -> ActiveServiceSnapshot:
    try:
        return ActiveServiceSnapshot(
            service=_service(row["service"]),
            repository_id=int(row["repository_id"]),
            installation_id=int(row["installation_id"]),
            owner=str(row["owner"]),
            repository=str(row["repository"]),
            full_name=str(row["full_name"]),
            default_branch=str(row["default_branch"]),
            state_revision=int(row["state_revision"]),
            snapshot=_snapshot_from_row(row),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubAnalysisStoreError("stored active service mapping is invalid") from exc


async def load_incident_context(
    tx: aiosqlite.Connection, *, incident_id: str | UUID
) -> DiagnosisIncidentContext:
    """Load a bounded diagnosis context from one incident's latest alert.

    Raw event payloads are intentionally not selected.  The result only uses
    fields already normalized by the ingest/incident engine.
    """

    incident_id_text = _uuid_text(incident_id, "incident_id")
    async with tx.execute(
        """
        SELECT i.incident_id, i.scope_key, i.title, i.summary, i.severity,
               i.status AS incident_status, i.alert_count, i.root_cause_hint,
               r.service, r.alertname, r.status AS event_status, r.labels_json,
               r.message
        FROM incidents AS i
        JOIN raw_events AS r ON r.event_id = (
            SELECT latest.event_id
            FROM raw_events AS latest
            WHERE latest.incident_id = i.incident_id
            ORDER BY latest.seq DESC
            LIMIT 1
        )
        WHERE i.incident_id = ?
        """,
        (incident_id_text,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise GitHubAnalysisBindingError("incident context was not found")

    scope_key = row["scope_key"] or "default"
    message = row["message"] or row["title"]
    try:
        return DiagnosisIncidentContext(
            incident_id=row["incident_id"],
            service=row["service"],
            alertname=row["alertname"],
            severity=row["severity"],
            status=row["event_status"],
            scope_key=scope_key,
            alert_count=row["alert_count"],
            message=message,
            labels=_json_object(row["labels_json"], "raw event labels"),
            summary=_nullable_text(row["summary"]),
            graph_root_cause_hint=_nullable_text(row["root_cause_hint"]),
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise GitHubAnalysisStoreError("stored incident context is invalid") from exc


async def _load_active_service_snapshot(
    tx: aiosqlite.Connection,
    *,
    service: str,
    snapshot_id: str | None = None,
) -> ActiveServiceSnapshot:
    """Internal active mapping lookup, optionally for one immutable snapshot."""

    normalized_service = _service(service)
    where_snapshot = ""
    parameters: tuple[str, ...]
    if snapshot_id is None:
        parameters = (normalized_service,)
    else:
        where_snapshot = " AND s.snapshot_id = ?"
        parameters = (normalized_service, _uuid_text(snapshot_id, "snapshot_id"))

    async with tx.execute(
        f"""
        SELECT m.service, r.repository_id, r.installation_id, r.owner,
               r.name AS repository, r.full_name, r.default_branch,
               COALESCE(v.revision, 0) AS state_revision,
               s.snapshot_id, s.commit_sha, s.tree_sha
        FROM github_service_mappings AS m
        JOIN github_repositories AS r ON r.repository_id = m.repository_id
        JOIN github_installations AS i ON i.installation_id = r.installation_id
        LEFT JOIN github_installation_state_versions AS v
            ON v.installation_id = i.installation_id
        JOIN github_snapshots AS s ON s.repository_id = r.repository_id
        WHERE m.service = ?
          AND r.is_selected = 1
          AND r.is_archived = 0
          AND i.status = 'active'
          {where_snapshot}
        ORDER BY s.created_at DESC, s.snapshot_id DESC
        LIMIT 1
        """,
        parameters,
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise GitHubAnalysisBindingError(
            "active service mapping with a persisted GitHub snapshot was not found"
        )
    return _active_snapshot_from_row(row)


async def load_active_service_snapshot(
    tx: aiosqlite.Connection,
    *,
    service: str,
    snapshot_id: str | UUID | None = None,
) -> ActiveServiceSnapshot:
    """Load one usable immutable snapshot for an active service mapping.

    Without ``snapshot_id`` this returns the newest snapshot. Supplying the
    immutable ID lets a later patch-preview operation rehydrate the exact
    snapshot that a persisted diagnosis cited rather than silently moving to a
    newer branch snapshot.
    """

    return await _load_active_service_snapshot(
        tx,
        service=service,
        snapshot_id=None if snapshot_id is None else str(snapshot_id),
    )


async def load_incident_analysis_binding(
    tx: aiosqlite.Connection, *, incident_id: str | UUID
) -> tuple[DiagnosisIncidentContext, ActiveServiceSnapshot]:
    """Resolve an incident to its active service mapping and newest snapshot."""

    incident = await load_incident_context(tx, incident_id=incident_id)
    binding = await load_active_service_snapshot(tx, service=incident.service)
    return incident, binding


def _sanitize_text(value: str, *, maximum: int) -> str:
    """Persist readable explanation text while omitting obvious secrets/code blocks."""

    if not isinstance(value, str):
        raise GitHubAnalysisStoreError("diagnosis text is invalid")
    text = _CODE_FENCE.sub("[source code omitted]", value)
    text = _PEM_BLOCK.sub("[secret omitted]", text)
    text = _GITHUB_TOKEN.sub("[GitHub token omitted]", text)
    text = _BEARER_TOKEN.sub("Bearer [token omitted]", text)
    text = _SECRET_ASSIGNMENT.sub("[secret omitted]", text)

    safe_lines = [
        "[source code omitted]" if _SOURCE_LIKE_LINE.match(line) else line
        for line in text.splitlines()
    ]
    normalized = " ".join(part.strip() for part in safe_lines if part.strip())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        normalized = "[details omitted]"
    return normalized[:maximum]


def _json_array(value: list[dict[str, Any]] | list[str]) -> str:
    """Serialize only the module's whitelisted projection with stable output."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_context_digest(request: DiagnosisRequest, supplied_digest: str | None) -> str:
    if supplied_digest is not None:
        if not isinstance(supplied_digest, str) or not _HEX_DIGEST.fullmatch(supplied_digest.lower()):
            raise GitHubAnalysisStoreError("source_context_digest must be a SHA-256 hex digest")
        return supplied_digest.lower()

    # Only immutable object metadata is hashed.  The excerpt text is never
    # serialized or incorporated into this persisted value.
    material = {
        "snapshot_id": str(request.snapshot.snapshot_id),
        "commit_sha": request.snapshot.commit_sha,
        "excerpts": [
            {
                "path": excerpt.file_path,
                "blob_sha": excerpt.blob_sha,
                "start_line": excerpt.start_line,
                "end_line": excerpt.end_line,
                "byte_count": excerpt.byte_count,
            }
            for excerpt in request.excerpts
        ],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_result(result: DiagnosisResult) -> DiagnosisResult:
    if not isinstance(result, DiagnosisResult):
        raise GitHubAnalysisStoreError("diagnosis result is invalid")
    try:
        return DiagnosisResult.model_validate(result.model_dump())
    except (ValidationError, TypeError, ValueError) as exc:
        raise GitHubAnalysisStoreError("diagnosis result is invalid") from exc


def _diagnosis_projection(result: DiagnosisResult) -> dict[str, Any]:
    """Return the only result fields allowed across the persistence boundary."""

    validated = _validated_result(result)
    evidence: list[dict[str, Any]] = []
    for item in validated.evidence:
        entry: dict[str, Any] = {
            "kind": item.kind,
            "explanation": _sanitize_text(item.explanation, maximum=1_000),
        }
        if item.kind == "source_excerpt":
            # Location metadata is useful for review, but excerpt content is
            # deliberately absent.
            entry.update(
                {
                    "file_path": item.file_path,
                    "blob_sha": item.blob_sha,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                }
            )
        evidence.append(entry)

    projection: dict[str, Any] = {
        "status": validated.status,
        "provider": validated.provider,
        "confidence": validated.confidence,
        "evidence": evidence,
        "root_cause_summary": None,
        "root_cause_reasoning": None,
        "proposed_fix_summary": None,
        "proposed_fix_steps": [],
        "proposed_fix_paths": [],
        "fallback_reason": None,
        "fallback_message": None,
        "fallback_next_steps": [],
    }
    if validated.status == "diagnosed":
        assert validated.root_cause_hypothesis is not None
        assert validated.proposed_fix is not None
        projection.update(
            {
                "root_cause_summary": _sanitize_text(
                    validated.root_cause_hypothesis.summary, maximum=2_000
                ),
                "root_cause_reasoning": _sanitize_text(
                    validated.root_cause_hypothesis.reasoning, maximum=4_000
                ),
                "proposed_fix_summary": _sanitize_text(
                    validated.proposed_fix.summary, maximum=2_000
                ),
                "proposed_fix_steps": [
                    _sanitize_text(step, maximum=1_000)
                    for step in validated.proposed_fix.steps
                ],
                "proposed_fix_paths": list(validated.proposed_fix.affected_paths),
            }
        )
    else:
        assert validated.fallback is not None
        projection.update(
            {
                "fallback_reason": validated.fallback.reason,
                "fallback_message": _sanitize_text(validated.fallback.message, maximum=2_000),
                "fallback_next_steps": [
                    _sanitize_text(step, maximum=1_000)
                    for step in validated.fallback.next_steps
                ],
            }
        )
    return projection


async def persist_diagnosis_result(
    tx: aiosqlite.Connection,
    *,
    request: DiagnosisRequest,
    result: DiagnosisResult,
    source_context_digest: str | None = None,
) -> dict[str, object]:
    """Append a sanitized result after rechecking service-to-snapshot scope.

    The request's excerpts are intentionally used only for a count, byte total,
    and metadata digest.  Their ``content`` field never reaches an SQL value.
    """

    if not isinstance(request, DiagnosisRequest):
        raise GitHubAnalysisStoreError("diagnosis request is invalid")
    try:
        validated_request = DiagnosisRequest.model_validate(request.model_dump())
    except (ValidationError, TypeError, ValueError) as exc:
        raise GitHubAnalysisStoreError("diagnosis request is invalid") from exc

    # Do not allow a caller to attach an existing incident ID to a different
    # service and thereby select an unrelated repository mapping.
    persisted_incident = await load_incident_context(
        tx, incident_id=validated_request.incident.incident_id
    )
    if persisted_incident.service != validated_request.incident.service:
        raise GitHubAnalysisBindingError(
            "diagnosis service does not match the stored incident context"
        )

    # This checks that the service is still mapped to an active, selected
    # repository and that the requested immutable snapshot belongs to it.
    binding = await _load_active_service_snapshot(
        tx,
        service=validated_request.incident.service,
        snapshot_id=str(validated_request.snapshot.snapshot_id),
    )
    if binding.snapshot != validated_request.snapshot:
        raise GitHubAnalysisBindingError(
            "diagnosis snapshot does not match the active service mapping"
        )

    incident_id_text = str(validated_request.incident.incident_id)
    async with tx.execute(
        "SELECT 1 AS found FROM incidents WHERE incident_id = ?", (incident_id_text,)
    ) as cursor:
        incident_exists = await cursor.fetchone() is not None
    if not incident_exists:
        raise GitHubAnalysisBindingError("diagnosis incident was not found")

    projection = _diagnosis_projection(result)
    digest = _safe_context_digest(validated_request, source_context_digest)
    analysis_id = str(uuid4())
    await tx.execute(
        """
        INSERT INTO github_incident_analyses (
            analysis_id, incident_id, service, repository_id, snapshot_id,
            status, provider, confidence,
            root_cause_summary, root_cause_reasoning,
            evidence_json,
            proposed_fix_summary, proposed_fix_steps_json, proposed_fix_paths_json,
            fallback_reason, fallback_message, fallback_next_steps_json,
            source_context_digest, source_excerpt_count, source_bytes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_id,
            incident_id_text,
            validated_request.incident.service,
            binding.repository_id,
            str(binding.snapshot.snapshot_id),
            projection["status"],
            projection["provider"],
            projection["confidence"],
            projection["root_cause_summary"],
            projection["root_cause_reasoning"],
            _json_array(projection["evidence"]),
            projection["proposed_fix_summary"],
            _json_array(projection["proposed_fix_steps"]),
            _json_array(projection["proposed_fix_paths"]),
            projection["fallback_reason"],
            projection["fallback_message"],
            _json_array(projection["fallback_next_steps"]),
            digest,
            len(validated_request.excerpts),
            validated_request.source_bytes,
        ),
    )
    return await get_diagnosis_result(tx, analysis_id=analysis_id)


def _json_list(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, str):
        raise GitHubAnalysisStoreError(f"stored {field_name} is invalid")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GitHubAnalysisStoreError(f"stored {field_name} is invalid") from exc
    if not isinstance(parsed, list):
        raise GitHubAnalysisStoreError(f"stored {field_name} is invalid")
    return parsed


def _record_from_row(row: aiosqlite.Row | dict[str, Any]) -> dict[str, object]:
    evidence = _json_list(row["evidence_json"], "evidence")
    proposed_steps = _json_list(row["proposed_fix_steps_json"], "proposed fix steps")
    proposed_paths = _json_list(row["proposed_fix_paths_json"], "proposed fix paths")
    fallback_steps = _json_list(row["fallback_next_steps_json"], "fallback next steps")

    status = row["status"]
    if status not in {"diagnosed", "fallback"}:
        raise GitHubAnalysisStoreError("stored analysis status is invalid")
    diagnosis: dict[str, object] = {
        "status": status,
        "provider": row["provider"],
        "confidence": row["confidence"],
        "root_cause_hypothesis": None,
        "evidence": evidence,
        "proposed_fix": None,
        "fallback": None,
    }
    if status == "diagnosed":
        diagnosis["root_cause_hypothesis"] = {
            "summary": row["root_cause_summary"],
            "reasoning": row["root_cause_reasoning"],
        }
        diagnosis["proposed_fix"] = {
            "summary": row["proposed_fix_summary"],
            "steps": proposed_steps,
            "affected_paths": proposed_paths,
            "requires_human_review": True,
            "automatically_applied": False,
        }
    else:
        diagnosis["fallback"] = {
            "reason": row["fallback_reason"],
            "message": row["fallback_message"],
            "next_steps": fallback_steps,
            "requires_human_review": True,
        }

    return {
        "analysis_id": row["analysis_id"],
        "incident_id": row["incident_id"],
        "service": row["service"],
        "repository_id": row["repository_id"],
        "snapshot_id": row["snapshot_id"],
        "diagnosis": diagnosis,
        "source_context": {
            "digest": row["source_context_digest"],
            "excerpt_count": row["source_excerpt_count"],
            "byte_count": row["source_bytes"],
        },
        "created_at": row["created_at"],
    }


async def get_diagnosis_result(
    tx: aiosqlite.Connection, *, analysis_id: str | UUID
) -> dict[str, object]:
    """Load one sanitized persisted diagnosis; source excerpts are unavailable."""

    analysis_id_text = _uuid_text(analysis_id, "analysis_id")
    async with tx.execute(
        """
        SELECT analysis_id, incident_id, service, repository_id, snapshot_id,
               status, provider, confidence,
               root_cause_summary, root_cause_reasoning, evidence_json,
               proposed_fix_summary, proposed_fix_steps_json, proposed_fix_paths_json,
               fallback_reason, fallback_message, fallback_next_steps_json,
               source_context_digest, source_excerpt_count, source_bytes, created_at
        FROM github_incident_analyses
        WHERE analysis_id = ?
        """,
        (analysis_id_text,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise GitHubAnalysisStoreError("diagnosis result was not found")
    return _record_from_row(row)


async def list_incident_diagnosis_results(
    tx: aiosqlite.Connection,
    *,
    incident_id: str | UUID,
    limit: int = 20,
) -> list[dict[str, object]]:
    """List newest sanitized diagnosis records for one incident."""

    incident_id_text = _uuid_text(incident_id, "incident_id")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise GitHubAnalysisStoreError("limit must be an integer")
    safe_limit = max(1, min(limit, 100))
    async with tx.execute(
        """
        SELECT analysis_id, incident_id, service, repository_id, snapshot_id,
               status, provider, confidence,
               root_cause_summary, root_cause_reasoning, evidence_json,
               proposed_fix_summary, proposed_fix_steps_json, proposed_fix_paths_json,
               fallback_reason, fallback_message, fallback_next_steps_json,
               source_context_digest, source_excerpt_count, source_bytes, created_at
        FROM github_incident_analyses
        WHERE incident_id = ?
        ORDER BY created_at DESC, analysis_id DESC
        LIMIT ?
        """,
        (incident_id_text, safe_limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_record_from_row(row) for row in rows]


__all__ = [
    "ActiveServiceSnapshot",
    "GitHubAnalysisBindingError",
    "GitHubAnalysisStoreError",
    "get_diagnosis_result",
    "list_incident_diagnosis_results",
    "load_active_service_snapshot",
    "load_incident_analysis_binding",
    "load_incident_context",
    "persist_diagnosis_result",
]
