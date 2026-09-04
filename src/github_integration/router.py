"""FastAPI routes for PulseGraph's deliberately read-only GitHub Phase 1."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
import hmac
import json
from typing import Any, TypeVar
from urllib.parse import quote

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.config import settings
from src.db.connection import Database, get_reader_connection
from src.github_integration.analysis_store import (
    GitHubAnalysisBindingError,
    GitHubAnalysisStoreError,
    get_diagnosis_result,
    list_incident_diagnosis_results,
    load_active_service_snapshot,
    load_incident_analysis_binding,
    load_incident_context,
    persist_diagnosis_result,
)
from src.github_integration.client import (
    GitHubIntegrationError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubReadOnlyClient,
    GitHubTreeLimitExceeded,
    RepositoryMetadata,
)
from src.github_integration.diagnosis import (
    MAX_EXCERPT_BYTES,
    MAX_SOURCE_EXCERPTS,
    MAX_TOTAL_EXCERPT_BYTES,
    DiagnosisResult,
)
from src.github_integration.ollama_provider import (
    OllamaLocalError,
    PatchSourceFile,
)
from src.github_integration.source_context import (
    SourceContext,
    SourceContextError,
    SourceContextPolicy,
    build_source_context,
)
from src.github_integration.store import (
    GitHubConnectionStateError,
    GitHubStoreError,
    create_snapshot,
    get_installation,
    get_repository,
    get_snapshot,
    get_snapshot_files_by_paths,
    list_repositories,
    list_snapshot_files,
    list_snapshot_source_inventory,
    record_webhook_delivery,
    require_active_selected_repository,
    replace_installation_repositories,
    set_service_mapping,
    set_webhook_delivery_status,
    sync_installation_repositories_from_webhook,
    upsert_installation_from_webhook,
    upsert_repository,
)
from src.github_integration.webhooks import payload_digest, verify_github_webhook_signature
from src.github_integration.workflow import (
    bind_patch_to_snapshot,
    build_diagnosis_request,
    patch_review_payload,
)
from src.github_integration.workspace import (
    LocalPatchWorkspace,
    PatchWorkspaceError,
    WorkspaceLimits,
)


_ALLOWED_INSTALLATION_ACTIONS = frozenset(
    {"created", "deleted", "suspend", "unsuspend", "new_permissions_accepted"}
)
_ALLOWED_WEBHOOK_EVENTS = frozenset({"installation", "installation_repositories"})
T = TypeVar("T")


class ServiceMappingRequest(BaseModel):
    repository_id: int = Field(gt=0)


def _admin_token_from_request(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and token:
        return token
    return None


async def require_github_admin(request: Request) -> None:
    """Protect repository metadata APIs until dashboard account auth exists."""

    expected = settings.GITHUB_ADMIN_TOKEN
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub admin API is disabled until GITHUB_ADMIN_TOKEN is configured",
        )
    provided = _admin_token_from_request(request)
    if provided is None or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def _database(request: Request) -> Database:
    database = getattr(request.app.state, "db", None)
    if not isinstance(database, Database) or database.writer_conn is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable")
    return database


def _github_client(request: Request) -> GitHubReadOnlyClient:
    client = getattr(request.app.state, "github_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub App credentials are not configured",
        )
    return client


async def _write(
    request: Request, operation: Callable[[aiosqlite.Connection], Awaitable[T]]
) -> T:
    """Run an integration write atomically on PulseGraph's one SQLite writer."""

    database = _database(request)
    assert database.writer_conn is not None
    async with database.write_lock:
        try:
            result = await operation(database.writer_conn)
            await database.writer_conn.commit()
            return result
        except Exception:
            await database.writer_conn.rollback()
            raise


def _installation_id(payload: Mapping[str, Any]) -> int | None:
    installation = payload.get("installation")
    if not isinstance(installation, Mapping):
        return None
    value = installation.get("id")
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _metadata_payload(metadata: RepositoryMetadata) -> dict[str, Any]:
    """Adapt the client's validated DTO to the persistence shape."""

    return {
        "id": metadata.id,
        "name": metadata.name,
        "full_name": metadata.full_name,
        "owner": {"login": metadata.owner},
        "default_branch": metadata.default_branch,
        "html_url": metadata.html_url or "",
        "private": metadata.private,
        "archived": False,
    }


def _github_failure(exc: GitHubIntegrationError) -> HTTPException:
    """Expose a safe operational status without leaking provider response bodies."""

    if isinstance(exc, GitHubNotFoundError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="GitHub resource is unavailable")
    if isinstance(exc, GitHubTreeLimitExceeded):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="repository source inventory exceeds the configured safe limit",
        )
    if isinstance(exc, GitHubRateLimitError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub rate limit reached; retry later",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="GitHub read operation failed",
    )


def _configured_positive_limit(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{name} is misconfigured",
        )
    return value


def _diagnosis_source_policy() -> SourceContextPolicy:
    """Build a policy that cannot exceed the diagnosis contract's budgets."""

    max_files = _configured_positive_limit(
        settings.GITHUB_DIAGNOSIS_MAX_FILES, "GITHUB_DIAGNOSIS_MAX_FILES"
    )
    max_file_bytes = _configured_positive_limit(
        settings.GITHUB_DIAGNOSIS_MAX_FILE_BYTES, "GITHUB_DIAGNOSIS_MAX_FILE_BYTES"
    )
    max_total_bytes = _configured_positive_limit(
        settings.GITHUB_DIAGNOSIS_MAX_TOTAL_BYTES, "GITHUB_DIAGNOSIS_MAX_TOTAL_BYTES"
    )
    if (
        max_files > MAX_SOURCE_EXCERPTS
        or max_file_bytes > MAX_EXCERPT_BYTES
        or max_total_bytes > MAX_TOTAL_EXCERPT_BYTES
        or max_file_bytes > max_total_bytes
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub diagnosis source limits exceed the supported safe bounds",
        )
    return SourceContextPolicy(
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )


def _patch_source_policy() -> SourceContextPolicy:
    """Build the bounded full-file policy used only for editable patch paths."""

    max_files = _configured_positive_limit(settings.GITHUB_PATCH_MAX_FILES, "GITHUB_PATCH_MAX_FILES")
    max_file_bytes = _configured_positive_limit(
        settings.GITHUB_PATCH_MAX_FILE_BYTES, "GITHUB_PATCH_MAX_FILE_BYTES"
    )
    max_total_bytes = _configured_positive_limit(
        settings.GITHUB_PATCH_MAX_TOTAL_BYTES, "GITHUB_PATCH_MAX_TOTAL_BYTES"
    )
    if max_files > 100 or max_file_bytes > max_total_bytes or max_total_bytes > 512 * 1024:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub patch source limits exceed the supported safe bounds",
        )
    return SourceContextPolicy(
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )


def _patch_workspace_limits() -> WorkspaceLimits:
    """Translate deployment budgets into the local-only review workspace."""

    max_files = _configured_positive_limit(settings.GITHUB_PATCH_MAX_FILES, "GITHUB_PATCH_MAX_FILES")
    max_file_bytes = _configured_positive_limit(
        settings.GITHUB_PATCH_MAX_FILE_BYTES, "GITHUB_PATCH_MAX_FILE_BYTES"
    )
    max_total_bytes = _configured_positive_limit(
        settings.GITHUB_PATCH_MAX_TOTAL_BYTES, "GITHUB_PATCH_MAX_TOTAL_BYTES"
    )
    max_changes = _configured_positive_limit(
        settings.GITHUB_PATCH_MAX_CHANGES, "GITHUB_PATCH_MAX_CHANGES"
    )
    max_patch_bytes = _configured_positive_limit(
        settings.GITHUB_PATCH_MAX_BYTES, "GITHUB_PATCH_MAX_BYTES"
    )
    max_diff_bytes = _configured_positive_limit(
        settings.GITHUB_PATCH_MAX_DIFF_BYTES, "GITHUB_PATCH_MAX_DIFF_BYTES"
    )
    try:
        return WorkspaceLimits(
            max_file_count=max_files,
            max_changes=max_changes,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
            max_patch_bytes=max_patch_bytes,
            max_diff_bytes=max_diff_bytes,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub patch workspace limits are misconfigured",
        ) from exc


def _snapshot_inventory_limit() -> int:
    limit = _configured_positive_limit(settings.GITHUB_MAX_TREE_ENTRIES, "GITHUB_MAX_TREE_ENTRIES")
    if limit > 20_000:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GITHUB_MAX_TREE_ENTRIES exceeds the supported source inventory bound",
        )
    return limit


def _diagnosis_service(request: Request) -> Any:
    service = getattr(request.app.state, "diagnosis_service", None)
    if service is None or not callable(getattr(service, "diagnose", None)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub diagnosis service is unavailable",
        )
    return service


def _ollama_provider(request: Request) -> Any:
    provider = getattr(request.app.state, "ollama_provider", None)
    if provider is None or not callable(getattr(provider, "propose_patch", None)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="local patch proposal model is not configured",
        )
    return provider


def _incident_signals(incident: Any) -> tuple[str, ...]:
    """Use normalized incident fields only; raw webhook payloads never enter prompts."""

    values = [
        str(incident.service),
        str(incident.alertname),
        str(incident.message),
        str(incident.scope_key),
    ]
    if incident.summary is not None:
        values.append(str(incident.summary))
    if incident.graph_root_cause_hint is not None:
        values.append(str(incident.graph_root_cause_hint))
    values.extend(str(key) for key in incident.labels)
    values.extend(str(value) for value in incident.labels.values())
    return tuple(values)


async def _build_source_context_for_binding(
    request: Request,
    *,
    binding: Any,
    snapshot_rows: Sequence[Mapping[str, object]],
    signals: Sequence[str],
    policy: SourceContextPolicy,
) -> SourceContext:
    """Read source only through a scoped installation token and pinned blobs."""

    client = _github_client(request)
    try:
        token = await client.create_installation_token(
            int(binding.installation_id), repository_ids=[int(binding.repository_id)]
        )
        return await build_source_context(
            client,
            owner=str(binding.owner),
            repository=str(binding.repository),
            token=token,
            snapshot_rows=snapshot_rows,
            signals=signals,
            policy=policy,
        )
    except SourceContextError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="pinned GitHub source context is unavailable or inconsistent",
        ) from exc
    except GitHubIntegrationError as exc:
        raise _github_failure(exc) from exc


async def _recheck_active_analysis_binding(binding: Any) -> None:
    """Fail closed if a signed lifecycle event changed scope during source I/O."""

    async with get_reader_connection(settings.DATABASE_PATH) as connection:
        try:
            current = await load_active_service_snapshot(
                connection,
                service=str(binding.service),
                snapshot_id=str(binding.snapshot.snapshot_id),
            )
        except GitHubAnalysisStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="GitHub repository mapping changed while source was being read",
            ) from exc
    if (
        current.repository_id != binding.repository_id
        or current.installation_id != binding.installation_id
        or current.snapshot != binding.snapshot
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="GitHub repository mapping changed while source was being read",
        )


def _analysis_binding_failure(exc: GitHubAnalysisStoreError) -> HTTPException:
    if isinstance(exc, GitHubAnalysisBindingError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="incident does not have an active mapped GitHub snapshot",
        )
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="GitHub incident analysis data is invalid",
    )


async def _read_bounded_webhook_body(request: Request) -> bytes:
    """Read raw signed bytes while bounding both declared and chunked bodies."""

    maximum = _configured_positive_limit(
        settings.GITHUB_WEBHOOK_MAX_BYTES, "GITHUB_WEBHOOK_MAX_BYTES"
    )
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            declared = int(declared_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="invalid Content-Length",
            ) from exc
        if declared < 0 or declared > maximum:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="GitHub webhook body exceeds the configured limit",
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="GitHub webhook body exceeds the configured limit",
            )
        body.extend(chunk)
    return bytes(body)


def _validate_webhook_repository_bounds(payload: Mapping[str, Any], event_type: str) -> None:
    maximum = _configured_positive_limit(
        settings.GITHUB_WEBHOOK_MAX_REPOSITORIES, "GITHUB_WEBHOOK_MAX_REPOSITORIES"
    )
    fields = ("repositories",) if event_type == "installation" else (
        "repositories_added",
        "repositories_removed",
    )
    for field in fields:
        value = payload.get(field, [])
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"GitHub webhook field {field} must be an array",
            )
        if len(value) > maximum:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"GitHub webhook field {field} exceeds the configured repository limit",
            )


def create_github_router() -> APIRouter:
    router = APIRouter(prefix="/v1/github", tags=["github"])

    @router.post("/webhooks", status_code=status.HTTP_202_ACCEPTED)
    async def receive_github_webhook(request: Request) -> dict[str, str]:
        """Verify and process only GitHub App lifecycle webhooks.

        No GitHub API call occurs inside this endpoint, keeping acknowledgement
        fast and making this path safe to expose publicly behind HTTPS.
        """

        if not settings.GITHUB_WEBHOOK_SECRET:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GitHub webhook handling is disabled until GITHUB_WEBHOOK_SECRET is configured",
            )
        body = await _read_bounded_webhook_body(request)
        signature = request.headers.get("x-hub-signature-256")
        if not verify_github_webhook_signature(
            secret=settings.GITHUB_WEBHOOK_SECRET, body=body, signature=signature
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid webhook signature")

        event_type = request.headers.get("x-github-event", "")
        delivery_id = request.headers.get("x-github-delivery", "")
        if not event_type or not delivery_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="GitHub delivery and event headers are required",
            )
        if len(delivery_id) > 128 or not delivery_id.isascii() or any(
            character.isspace() for character in delivery_id
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="GitHub delivery ID is invalid",
            )
        if event_type not in _ALLOWED_WEBHOOK_EVENTS:
            return {"status": "ignored"}
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="invalid GitHub webhook JSON",
            ) from exc
        if not isinstance(payload, Mapping):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="GitHub webhook JSON must be an object",
            )
        _validate_webhook_repository_bounds(payload, event_type)
        action = payload.get("action")
        if action is not None and not isinstance(action, str):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="GitHub webhook action must be a string",
            )

        async def process(tx: aiosqlite.Connection) -> str:
            is_new = await record_webhook_delivery(
                tx,
                delivery_id=delivery_id,
                event_type=event_type,
                action=action,
                installation_id=_installation_id(payload),
                payload_sha256=payload_digest(body),
            )
            if not is_new:
                return "duplicate"

            if event_type == "installation":
                if action not in _ALLOWED_INSTALLATION_ACTIONS:
                    await set_webhook_delivery_status(tx, delivery_id, "ignored")
                    return "ignored"
                installation_id, lifecycle_event_applied = await upsert_installation_from_webhook(
                    tx, payload, action
                )
                if not lifecycle_event_applied:
                    await set_webhook_delivery_status(tx, delivery_id, "ignored")
                    return "ignored"
                installation = await get_installation(tx, installation_id)
                repositories = payload.get("repositories", [])
                if installation["status"] == "active" and isinstance(repositories, Sequence) and not isinstance(
                    repositories, (str, bytes)
                ):
                    for repository in repositories:
                        if not isinstance(repository, Mapping):
                            raise GitHubStoreError("installation repository entry must be an object")
                        await upsert_repository(
                            tx, installation_id=installation_id, repository=repository
                        )
                await set_webhook_delivery_status(tx, delivery_id, "processed")
                return "processed"

            installation_id = _installation_id(payload)
            if installation_id is None:
                raise GitHubStoreError("installation_repositories webhook is missing installation.id")
            _, installation_status, state_event_applied = await sync_installation_repositories_from_webhook(
                tx, payload
            )
            if installation_status != "active" or not state_event_applied:
                await set_webhook_delivery_status(tx, delivery_id, "ignored")
                return "ignored"
            await set_webhook_delivery_status(tx, delivery_id, "processed")
            return "processed"

        try:
            outcome = await _write(request, process)
        except GitHubStoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid GitHub webhook: {exc}",
            ) from exc
        return {"status": outcome}

    @router.get("/install-url", dependencies=[Depends(require_github_admin)])
    async def github_install_url() -> dict[str, str]:
        if not settings.GITHUB_APP_SLUG:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GITHUB_APP_SLUG is not configured",
            )
        return {
            "install_url": "https://github.com/apps/"
            f"{quote(settings.GITHUB_APP_SLUG, safe='-')}/installations/new"
        }

    @router.get("/repositories", dependencies=[Depends(require_github_admin)])
    async def github_repositories() -> dict[str, list[dict[str, Any]]]:
        async with get_reader_connection(settings.DATABASE_PATH) as connection:
            return {"repositories": await list_repositories(connection)}

    @router.post(
        "/installations/{installation_id}/sync",
        dependencies=[Depends(require_github_admin)],
    )
    async def sync_github_installation(request: Request, installation_id: int) -> dict[str, Any]:
        if installation_id <= 0:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid installation ID")
        async with get_reader_connection(settings.DATABASE_PATH) as connection:
            try:
                installation = await get_installation(connection, installation_id)
            except GitHubStoreError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        if installation["status"] != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="GitHub installation is not a selected-repository read-only installation",
            )

        client = _github_client(request)
        try:
            token = await client.create_installation_token(installation_id)
            remote_repositories = await client.list_installation_repositories(token)
        except GitHubIntegrationError as exc:
            raise _github_failure(exc) from exc

        async def persist(tx: aiosqlite.Connection) -> tuple[int, ...]:
            return await replace_installation_repositories(
                tx,
                installation_id=installation_id,
                expected_state_revision=int(installation["state_revision"]),
                repositories=[_metadata_payload(repository) for repository in remote_repositories],
            )

        try:
            repository_ids = await _write(request, persist)
        except GitHubConnectionStateError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="GitHub installation changed while refresh was in progress; retry the sync",
            ) from exc
        return {"status": "ok", "installation_id": installation_id, "repository_ids": repository_ids}

    @router.put(
        "/service-mappings/{service}",
        dependencies=[Depends(require_github_admin)],
    )
    async def map_service_to_repository(
        request: Request, service: str, body: ServiceMappingRequest
    ) -> dict[str, Any]:
        try:
            return await _write(
                request,
                lambda tx: set_service_mapping(
                    tx, service=service, repository_id=body.repository_id
                ),
            )
        except GitHubStoreError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    @router.post(
        "/repositories/{repository_id}/snapshots",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_github_admin)],
    )
    async def create_github_snapshot(request: Request, repository_id: int) -> dict[str, Any]:
        if repository_id <= 0:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid repository ID")
        async with get_reader_connection(settings.DATABASE_PATH) as connection:
            try:
                repository = await get_repository(connection, repository_id)
            except GitHubStoreError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        if not repository["is_selected"] or repository["installation_status"] != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="repository is not selected in an active read-only installation",
            )

        client = _github_client(request)
        try:
            token = await client.create_installation_token(
                int(repository["installation_id"]), repository_ids=[repository_id]
            )
            remote_repository = await client.get_repository_metadata(
                str(repository["owner"]), str(repository["name"]), token
            )
            if remote_repository.id != repository_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="GitHub repository identity changed; refusing to snapshot",
                )
            branch = await client.get_branch(
                remote_repository.owner,
                remote_repository.name,
                remote_repository.default_branch,
                token,
            )
            tree = await client.get_complete_tree(
                remote_repository.owner,
                remote_repository.name,
                branch.tree_sha,
                token,
                max_entries=settings.GITHUB_MAX_TREE_ENTRIES,
                max_tree_requests=settings.GITHUB_MAX_TREE_REQUESTS,
                max_tree_depth=settings.GITHUB_MAX_TREE_DEPTH,
            )
        except HTTPException:
            raise
        except GitHubIntegrationError as exc:
            raise _github_failure(exc) from exc

        async def persist(tx: aiosqlite.Connection) -> dict[str, Any]:
            # Recheck selection before metadata refresh. Otherwise a removal
            # webhook received during remote reads could be undone by the
            # following upsert setting is_selected back to true.
            await require_active_selected_repository(tx, repository_id)
            await upsert_repository(
                tx,
                installation_id=int(repository["installation_id"]),
                repository=_metadata_payload(remote_repository),
            )
            return await create_snapshot(
                tx,
                repository_id=repository_id,
                ref=branch.name,
                commit_sha=branch.commit_sha,
                tree_sha=branch.tree_sha,
                tree_entries=[
                    {
                        "path": entry.path,
                        "mode": entry.mode,
                        "type": entry.type,
                        "sha": entry.sha,
                        "size": entry.size,
                    }
                    for entry in tree.entries
                ],
                tree_truncated=tree.truncated,
            )

        try:
            return await _write(request, persist)
        except GitHubConnectionStateError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="GitHub repository selection changed while snapshotting; retry after reconnecting it",
            ) from exc

    @router.get("/snapshots/{snapshot_id}", dependencies=[Depends(require_github_admin)])
    async def github_snapshot(
        snapshot_id: str, include_files: bool = False, file_limit: int = 500
    ) -> dict[str, Any]:
        async with get_reader_connection(settings.DATABASE_PATH) as connection:
            try:
                snapshot = await get_snapshot(connection, snapshot_id)
            except GitHubStoreError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            if include_files:
                snapshot["files"] = await list_snapshot_files(
                    connection, snapshot_id, limit=file_limit
                )
        return snapshot

    @router.post(
        "/incidents/{incident_id}/diagnoses",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_github_admin)],
    )
    async def diagnose_github_incident(request: Request, incident_id: str) -> dict[str, object]:
        """Produce one bounded diagnosis for an incident's pinned repository snapshot.

        The source bundle exists only while this request runs. The persisted
        analysis record intentionally contains citation metadata and a
        sanitised conclusion, never source excerpts or provider payloads.
        """

        policy = _diagnosis_source_policy()
        async with get_reader_connection(settings.DATABASE_PATH) as connection:
            try:
                incident, binding = await load_incident_analysis_binding(
                    connection, incident_id=incident_id
                )
                snapshot_rows = await list_snapshot_source_inventory(
                    connection,
                    str(binding.snapshot.snapshot_id),
                    limit=_snapshot_inventory_limit(),
                )
            except GitHubAnalysisStoreError as exc:
                raise _analysis_binding_failure(exc) from exc
            except GitHubStoreError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="pinned GitHub snapshot inventory is unavailable",
                ) from exc

        source_context = await _build_source_context_for_binding(
            request,
            binding=binding,
            snapshot_rows=snapshot_rows,
            signals=_incident_signals(incident),
            policy=policy,
        )
        await _recheck_active_analysis_binding(binding)
        try:
            diagnosis_request = build_diagnosis_request(
                incident=incident,
                snapshot=binding.snapshot,
                source_context=source_context,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="pinned GitHub source could not be used as bounded diagnosis evidence",
            ) from exc

        try:
            result = await _diagnosis_service(request).diagnose(diagnosis_request)
            result = DiagnosisResult.model_validate(result.model_dump())
        except HTTPException:
            raise
        except Exception as exc:
            # Providers receive source text, so their implementation failures
            # must never be echoed into an API response or log message here.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GitHub diagnosis provider is unavailable",
            ) from exc

        try:
            return await _write(
                request,
                lambda tx: persist_diagnosis_result(
                    tx,
                    request=diagnosis_request,
                    result=result,
                    source_context_digest=source_context.digest,
                ),
            )
        except GitHubAnalysisStoreError as exc:
            # The binding is rechecked inside the writer transaction. A
            # revocation or remapping received during the read/model phase
            # therefore prevents persistence of a stale investigation.
            raise _analysis_binding_failure(exc) from exc

    @router.get(
        "/incidents/{incident_id}/diagnoses",
        dependencies=[Depends(require_github_admin)],
    )
    async def list_github_incident_diagnoses(
        incident_id: str, limit: int = 20
    ) -> dict[str, list[dict[str, object]]]:
        async with get_reader_connection(settings.DATABASE_PATH) as connection:
            try:
                records = await list_incident_diagnosis_results(
                    connection, incident_id=incident_id, limit=limit
                )
            except GitHubAnalysisStoreError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="GitHub incident analysis request is invalid",
                ) from exc
        return {"analyses": records}

    @router.get(
        "/analyses/{analysis_id}", dependencies=[Depends(require_github_admin)]
    )
    async def github_incident_analysis(analysis_id: str) -> dict[str, object]:
        async with get_reader_connection(settings.DATABASE_PATH) as connection:
            try:
                return await get_diagnosis_result(connection, analysis_id=analysis_id)
            except GitHubAnalysisStoreError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="GitHub incident analysis was not found",
                ) from exc

    @router.post(
        "/analyses/{analysis_id}/patch-preview",
        dependencies=[Depends(require_github_admin)],
    )
    async def create_github_patch_preview(
        request: Request, analysis_id: str
    ) -> dict[str, object]:
        """Ask the optional local model for a disposable, reviewable diff.

        This route has no GitHub mutation capability. It rehydrates only the
        exact active snapshot cited by the saved analysis, fetches the small
        set of diagnosis-approved files through a restricted read token, and
        removes the temporary workspace before responding.
        """

        diagnosis_policy = _diagnosis_source_policy()
        patch_policy = _patch_source_policy()
        workspace_limits = _patch_workspace_limits()
        async with get_reader_connection(settings.DATABASE_PATH) as connection:
            try:
                analysis = await get_diagnosis_result(connection, analysis_id=analysis_id)
                diagnosis = DiagnosisResult.model_validate(analysis["diagnosis"])
                if diagnosis.status != "diagnosed" or diagnosis.proposed_fix is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="a grounded diagnosis is required before a patch preview can be created",
                    )
                incident = await load_incident_context(
                    connection, incident_id=str(analysis["incident_id"])
                )
                binding = await load_active_service_snapshot(
                    connection,
                    service=incident.service,
                    snapshot_id=str(analysis["snapshot_id"]),
                )
                if int(analysis["repository_id"]) != binding.repository_id:
                    raise GitHubAnalysisBindingError(
                        "analysis repository no longer matches its active service mapping"
                    )
                target_paths = tuple(dict.fromkeys(diagnosis.proposed_fix.affected_paths))
                if not target_paths:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="the grounded diagnosis did not identify editable source paths",
                    )
                evidence_paths = tuple(
                    dict.fromkeys(
                        evidence.file_path
                        for evidence in diagnosis.evidence
                        if evidence.kind == "source_excerpt" and evidence.file_path is not None
                    )
                )
                source_paths = tuple(dict.fromkeys((*evidence_paths, *target_paths)))
                if len(source_paths) > patch_policy.max_files:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="the diagnosis exceeds the configured patch source-file limit",
                    )
                if len(source_paths) > diagnosis_policy.max_files:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="the saved diagnosis exceeds its original bounded source-file limit",
                    )
                snapshot_rows = await get_snapshot_files_by_paths(
                    connection,
                    str(binding.snapshot.snapshot_id),
                    paths=source_paths,
                )
            except HTTPException:
                raise
            except GitHubAnalysisBindingError as exc:
                raise _analysis_binding_failure(exc) from exc
            except GitHubAnalysisStoreError as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="GitHub incident analysis was not found",
                ) from exc
            except GitHubStoreError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="pinned GitHub patch source inventory is unavailable",
                ) from exc

        snapshot_paths = {str(row["path"]) for row in snapshot_rows}
        if snapshot_paths != set(source_paths):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="one or more diagnosis evidence files are absent from the pinned snapshot",
            )

        patch_context = await _build_source_context_for_binding(
            request,
            binding=binding,
            snapshot_rows=snapshot_rows,
            signals=source_paths,
            policy=patch_policy,
        )
        await _recheck_active_analysis_binding(binding)
        source_by_path = {excerpt.path: excerpt for excerpt in patch_context.excerpts}
        if set(source_by_path) != set(source_paths):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="one or more diagnosis evidence files cannot be safely read from the pinned snapshot",
            )

        # A persisted diagnosis was created from the diagnosis policy. Rebuild
        # that same bounded evidence shape before passing it with the full
        # editable source to the local patch provider.
        try:
            diagnosis_context = SourceContext(
                excerpts=tuple(source_by_path[path] for path in source_paths),
                omitted_file_count=0,
                total_bytes=sum(source_by_path[path].byte_count for path in source_paths),
                digest=patch_context.digest,
            )
            patch_request = build_diagnosis_request(
                incident=incident,
                snapshot=binding.snapshot,
                source_context=diagnosis_context,
            )
            # Do not let a later patch phase broaden the diagnosis evidence:
            # source excerpts must remain within the original diagnosis cap.
            if patch_request.source_bytes > diagnosis_policy.max_total_bytes:
                raise ValueError("patch source exceeds the diagnosis evidence budget")
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="the saved diagnosis cannot be safely rehydrated from the pinned source",
            ) from exc

        editable_files = tuple(
            PatchSourceFile(
                path=path,
                blob_sha=source_by_path[path].blob_sha,
                content=source_by_path[path].text,
            )
            for path in target_paths
        )
        try:
            proposal = await _ollama_provider(request).propose_patch(
                patch_request,
                diagnosis,
                editable_files,
                patch_id=f"analysis-{analysis_id}",
            )
            base_files = {path: source_by_path[path].text for path in target_paths}
            scoped_proposal = bind_patch_to_snapshot(
                proposal,
                base_files=base_files,
                allowed_paths=target_paths,
            )
            with LocalPatchWorkspace(base_files, limits=workspace_limits) as workspace:
                review = workspace.apply(scoped_proposal)
        except HTTPException:
            raise
        except OllamaLocalError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="local model could not produce a reviewable patch preview",
            ) from exc
        except PatchWorkspaceError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="local model patch did not satisfy the review constraints",
            ) from exc
        except Exception as exc:
            # A substitute local provider in deployment or a temporary
            # workspace failure must not leak source or model internals.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="patch preview could not be created safely",
            ) from exc

        return {
            "analysis_id": analysis_id,
            "snapshot_id": str(binding.snapshot.snapshot_id),
            "human_review_required": True,
            "automatically_applied": False,
            "patch": patch_review_payload(review),
        }

    return router
