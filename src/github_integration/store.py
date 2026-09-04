"""SQLite persistence for the read-only GitHub Phase 1 boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4

import aiosqlite


class GitHubStoreError(ValueError):
    """Raised when GitHub metadata cannot safely be persisted."""


class GitHubConnectionStateError(GitHubStoreError):
    """Raised when a revoked or misconfigured binding is used after remote I/O."""


_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_DELETED_EVENT_PRIORITY = 100
_SUSPENDED_EVENT_PRIORITY = 90
_ALL_REPOSITORIES_EVENT_PRIORITY = 80
_NORMAL_EVENT_PRIORITY = 10


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GitHubStoreError(f"{context} must be an object")
    return value


def _text(value: object, context: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise GitHubStoreError(f"{context} must be a non-empty string")
    return value.strip()


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool):
        raise GitHubStoreError(f"{context} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise GitHubStoreError(f"{context} must be an integer") from exc
    if result <= 0:
        raise GitHubStoreError(f"{context} must be positive")
    return result


def _bool(value: object) -> int:
    return int(bool(value))


def _provider_timestamp_microseconds(value: object, context: str) -> int:
    """Parse GitHub's provider timestamp without relying on arrival order."""

    # GitHub's current REST payloads use RFC 3339 strings, while established
    # GitHub App webhook fixtures also contain integer Unix seconds. Accept
    # both documented shapes, but never use local arrival time as a fallback.
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise GitHubStoreError(f"{context} must not be negative")
        return value * 1_000_000
    text = _text(value, context)
    if text.isdigit():
        return int(text) * 1_000_000
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubStoreError(f"{context} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise GitHubStoreError(f"{context} must include a timezone")
    utc_value = parsed.astimezone(timezone.utc)
    delta = utc_value - _UNIX_EPOCH
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000) + delta.microseconds


def _event_priority(*, action: str, repository_selection: str | None = None) -> int:
    """Give same-timestamp revocations deterministic deny-wins precedence."""

    if action == "deleted":
        return _DELETED_EVENT_PRIORITY
    if action == "suspend":
        return _SUSPENDED_EVENT_PRIORITY
    if repository_selection == "all":
        return _ALL_REPOSITORIES_EVENT_PRIORITY
    if action == "removed":
        return 70
    return _NORMAL_EVENT_PRIORITY


def _installation_status(
    *, action: str, repository_selection: str, suspended_at: str | None, permissions: Mapping[str, Any]
) -> str:
    """Fail closed if an installation is broader than the read-only MVP needs."""

    if action == "deleted":
        return "deleted"
    if suspended_at:
        return "suspended"
    allowed_permissions = {"metadata": "read", "contents": "read"}
    if repository_selection != "selected":
        return "repository_selection_required"
    if dict(permissions) != allowed_permissions:
        return "permission_misconfigured"
    return "active"


async def _observe_installation_event(
    tx: aiosqlite.Connection,
    *,
    installation_id: int,
    provider_updated_at_us: int,
    event_priority: int,
) -> tuple[bool, int]:
    """Record a provider-ordered lifecycle/selection event.

    GitHub explicitly permits delivery reordering. A newer provider timestamp
    wins; at the same timestamp the higher, revoking priority wins. Returning
    ``False`` means the payload is stale and must not mutate connection state.
    """

    async with tx.execute(
        """
        SELECT provider_updated_at_us, provider_event_priority, revision
        FROM github_installation_state_versions
        WHERE installation_id = ?
        """,
        (installation_id,),
    ) as cursor:
        existing = await cursor.fetchone()

    if existing is None:
        await tx.execute(
            """
            INSERT INTO github_installation_state_versions (
                installation_id, provider_updated_at_us, provider_event_priority, revision
            ) VALUES (?, ?, ?, 1)
            """,
            (installation_id, provider_updated_at_us, event_priority),
        )
        return True, 1

    previous_timestamp = int(existing["provider_updated_at_us"])
    previous_priority = int(existing["provider_event_priority"])
    previous_revision = int(existing["revision"])
    should_apply = provider_updated_at_us > previous_timestamp or (
        provider_updated_at_us == previous_timestamp and event_priority > previous_priority
    )
    if not should_apply:
        return False, previous_revision

    next_revision = previous_revision + 1
    await tx.execute(
        """
        UPDATE github_installation_state_versions
        SET provider_updated_at_us = ?, provider_event_priority = ?, revision = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE installation_id = ?
        """,
        (provider_updated_at_us, event_priority, next_revision, installation_id),
    )
    return True, next_revision


def _repository_fields(repository: Mapping[str, Any]) -> dict[str, Any]:
    name = _text(repository.get("name"), "repository.name")
    full_name = _text(repository.get("full_name"), "repository.full_name")
    owner_value = repository.get("owner")
    if isinstance(owner_value, Mapping):
        owner_login = _text(owner_value.get("login"), "repository.owner.login")
        if full_name != f"{owner_login}/{name}":
            raise GitHubStoreError("repository.full_name does not match owner and name")
    else:
        # GitHub's installation webhook uses a compact repository object that
        # can omit ``owner`` and ``default_branch``. ``full_name`` is still
        # authoritative and lets us safely retain the binding until a later
        # read-only metadata refresh enriches it.
        owner_login, separator, parsed_name = full_name.partition("/")
        if not separator or not owner_login or parsed_name != name:
            raise GitHubStoreError("repository.full_name does not match owner and name")

    return {
        "repository_id": _integer(repository.get("id"), "repository.id"),
        "owner": owner_login,
        "name": name,
        "full_name": full_name,
        # Some lifecycle payloads omit default_branch. The snapshot route always
        # refreshes metadata before relying on it, so retain an existing value.
        "default_branch": _text(
            repository.get("default_branch", ""),
            "repository.default_branch",
            allow_empty=True,
        ),
        "html_url": str(repository.get("html_url") or ""),
        "is_private": _bool(repository.get("private", True)),
        "is_archived": _bool(repository.get("archived", False)),
    }


async def record_webhook_delivery(
    tx: aiosqlite.Connection,
    *,
    delivery_id: str,
    event_type: str,
    action: str | None,
    installation_id: int | None,
    payload_sha256: str,
) -> bool:
    """Record a delivery once and return ``False`` for a GitHub replay."""

    await tx.execute(
        """
        INSERT OR IGNORE INTO github_webhook_deliveries (
            delivery_id, event_type, action, installation_id, payload_sha256
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (delivery_id, event_type, action, installation_id, payload_sha256),
    )
    async with tx.execute("SELECT changes() AS changed") as cursor:
        row = await cursor.fetchone()
    return bool(row["changed"])


async def set_webhook_delivery_status(
    tx: aiosqlite.Connection, delivery_id: str, status: str
) -> None:
    await tx.execute(
        """
        UPDATE github_webhook_deliveries
        SET processing_status = ?, processed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE delivery_id = ?
        """,
        (status, delivery_id),
    )


async def upsert_installation_from_webhook(
    tx: aiosqlite.Connection, payload: Mapping[str, Any], action: str
) -> tuple[int, bool]:
    """Persist an installation lifecycle event without retaining credentials."""

    installation = _mapping(payload.get("installation"), "installation")
    account = _mapping(installation.get("account"), "installation.account")
    installation_id = _integer(installation.get("id"), "installation.id")
    provider_updated_at_us = _provider_timestamp_microseconds(
        installation.get("updated_at"), "installation.updated_at"
    )
    account_login = _text(account.get("login"), "installation.account.login")
    account_type = _text(account.get("type"), "installation.account.type")
    repository_selection = _text(
        installation.get("repository_selection", "selected"),
        "installation.repository_selection",
    )
    if repository_selection not in {"selected", "all"}:
        raise GitHubStoreError("installation.repository_selection must be 'selected' or 'all'")
    suspended_at = installation.get("suspended_at")
    if suspended_at is not None and not isinstance(suspended_at, str):
        raise GitHubStoreError("installation.suspended_at must be a string or null")
    permissions = installation.get("permissions", {})
    if not isinstance(permissions, Mapping):
        raise GitHubStoreError("installation.permissions must be an object")
    status = _installation_status(
        action=action,
        repository_selection=repository_selection,
        suspended_at=suspended_at,
        permissions=permissions,
    )

    async with tx.execute(
        "SELECT 1 AS found FROM github_installations WHERE installation_id = ?",
        (installation_id,),
    ) as cursor:
        exists = await cursor.fetchone() is not None
    if exists:
        existing = await get_installation(tx, installation_id)
        # Installation IDs are never reused. Once deleted, accepting a later
        # stale `created` delivery would be an unsafe resurrection. Likewise,
        # a suspended installation can be re-enabled only by a genuine
        # `unsuspend` lifecycle event that passes the provider timestamp check.
        if existing["status"] == "deleted" and action != "deleted":
            return installation_id, False
        if existing["status"] == "suspended" and action not in {"suspend", "unsuspend", "deleted"}:
            return installation_id, False
        applied, _ = await _observe_installation_event(
            tx,
            installation_id=installation_id,
            provider_updated_at_us=provider_updated_at_us,
            event_priority=_event_priority(
                action=action, repository_selection=repository_selection
            ),
        )
        if not applied:
            return installation_id, False

    await tx.execute(
        """
        INSERT INTO github_installations (
            installation_id, account_login, account_type, repository_selection,
            status, suspended_at, permissions_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(installation_id) DO UPDATE SET
            account_login = excluded.account_login,
            account_type = excluded.account_type,
            repository_selection = excluded.repository_selection,
            status = excluded.status,
            suspended_at = excluded.suspended_at,
            permissions_json = excluded.permissions_json,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (
            installation_id,
            account_login,
            account_type,
            repository_selection,
            status,
            suspended_at,
            json.dumps(dict(permissions), sort_keys=True, separators=(",", ":")),
        ),
    )

    if not exists:
        await _observe_installation_event(
            tx,
            installation_id=installation_id,
            provider_updated_at_us=provider_updated_at_us,
            event_priority=_event_priority(
                action=action, repository_selection=repository_selection
            ),
        )

    if status != "active":
        await tx.execute(
            """
            UPDATE github_repositories
            SET is_selected = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE installation_id = ?
            """,
            (installation_id,),
        )
    return installation_id, True


async def upsert_repository(
    tx: aiosqlite.Connection,
    *,
    installation_id: int,
    repository: Mapping[str, Any],
    is_selected: bool = True,
) -> int:
    """Upsert selected repository metadata received from GitHub."""

    fields = _repository_fields(repository)
    await tx.execute(
        """
        INSERT INTO github_repositories (
            repository_id, installation_id, owner, name, full_name, default_branch,
            html_url, is_private, is_archived, is_selected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repository_id) DO UPDATE SET
            installation_id = excluded.installation_id,
            owner = excluded.owner,
            name = excluded.name,
            full_name = excluded.full_name,
            default_branch = CASE
                WHEN excluded.default_branch <> '' THEN excluded.default_branch
                ELSE github_repositories.default_branch
            END,
            html_url = excluded.html_url,
            is_private = excluded.is_private,
            is_archived = excluded.is_archived,
            is_selected = excluded.is_selected,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (
            fields["repository_id"],
            installation_id,
            fields["owner"],
            fields["name"],
            fields["full_name"],
            fields["default_branch"],
            fields["html_url"],
            fields["is_private"],
            fields["is_archived"],
            _bool(is_selected),
        ),
    )
    return int(fields["repository_id"])


async def require_active_installation(
    tx: aiosqlite.Connection, installation_id: int
) -> dict[str, Any]:
    """Revalidate that a selected-repository read-only installation is active.

    Remote GitHub calls happen outside PulseGraph's SQLite writer lock. This
    check is deliberately performed *inside* the final write transaction so a
    signed suspension/deletion webhook cannot be undone by a stale sync result.
    """

    installation = await get_installation(tx, installation_id)
    if installation["status"] != "active":
        raise GitHubConnectionStateError(
            "GitHub installation is no longer an active selected-repository read-only installation"
        )
    return installation


async def require_active_selected_repository(
    tx: aiosqlite.Connection, repository_id: int
) -> dict[str, Any]:
    """Revalidate repository selection inside the final persistence transaction."""

    repository = await get_repository(tx, repository_id)
    if not repository["is_selected"] or repository["installation_status"] != "active":
        raise GitHubConnectionStateError(
            "repository is no longer selected in an active read-only installation"
        )
    return repository


async def sync_installation_repositories_from_webhook(
    tx: aiosqlite.Connection, payload: Mapping[str, Any]
) -> tuple[int, str, bool]:
    """Apply an ``installation_repositories`` webhook atomically."""

    installation = _mapping(payload.get("installation"), "installation")
    action = _text(payload.get("action"), "action")
    if action not in {"added", "removed"}:
        raise GitHubStoreError("installation_repositories action must be 'added' or 'removed'")
    repository_selection = _text(
        payload.get("repository_selection"), "repository_selection"
    )
    nested_selection = _text(
        installation.get("repository_selection"), "installation.repository_selection"
    )
    if repository_selection != nested_selection:
        raise GitHubStoreError(
            "repository_selection does not match installation.repository_selection"
        )
    added = payload.get("repositories_added", [])
    removed = payload.get("repositories_removed", [])
    if not isinstance(added, Sequence) or isinstance(added, (str, bytes)):
        raise GitHubStoreError("repositories_added must be a list")
    if not isinstance(removed, Sequence) or isinstance(removed, (str, bytes)):
        raise GitHubStoreError("repositories_removed must be a list")

    installation_id, state_event_applied = await upsert_installation_from_webhook(
        tx, payload, action
    )
    current_installation = await get_installation(tx, installation_id)
    if current_installation["status"] != "active":
        return installation_id, str(current_installation["status"]), state_event_applied

    # Only provider-current selection events may mutate selected repositories.
    # A stale add/remove is acknowledged but deferred to explicit, scoped sync.
    if not state_event_applied:
        return installation_id, "active", False

    for repository in added:
        await upsert_repository(
            tx,
            installation_id=installation_id,
            repository=_mapping(repository, "repositories_added entry"),
        )
    for repository in removed:
        fields = _repository_fields(_mapping(repository, "repositories_removed entry"))
        await tx.execute(
            """
            UPDATE github_repositories
            SET is_selected = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE repository_id = ? AND installation_id = ?
            """,
            (fields["repository_id"], installation_id),
        )
    return installation_id, "active", True


async def replace_installation_repositories(
    tx: aiosqlite.Connection,
    *,
    installation_id: int,
    expected_state_revision: int,
    repositories: Sequence[Mapping[str, Any]],
) -> tuple[int, ...]:
    """Make GitHub's selected-repository list authoritative after an API sync."""

    if isinstance(expected_state_revision, bool) or expected_state_revision < 0:
        raise GitHubStoreError("expected_state_revision must be a non-negative integer")
    installation = await require_active_installation(tx, installation_id)
    if installation["state_revision"] != expected_state_revision:
        raise GitHubConnectionStateError(
            "GitHub installation selection changed while the remote inventory was loading"
        )

    await tx.execute(
        """
        UPDATE github_repositories
        SET is_selected = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE installation_id = ?
        """,
        (installation_id,),
    )
    synced_ids: list[int] = []
    for repository in repositories:
        synced_ids.append(
            await upsert_repository(
                tx,
                installation_id=installation_id,
                repository=repository,
                is_selected=True,
            )
        )
    return tuple(synced_ids)


async def list_repositories(tx: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return selected repository bindings and the services mapped to each.

    The mapping is many-to-one: ``service`` is the primary key of
    ``github_service_mappings``, so several monitored services can point at one
    repository - a monorepo, or two alert sources backed by the same code.
    Joining the mappings into this query multiplied the repository row once per
    mapping, and the dashboard then rendered the same repository twice under
    the same React key. The mappings are read separately and grouped here, so
    one repository is always one row.
    """

    async with tx.execute(
        """
        SELECT r.repository_id, r.installation_id, r.owner, r.name, r.full_name,
               r.default_branch, r.html_url, r.is_private, r.is_archived,
               r.is_selected, r.last_seen_commit_sha, r.updated_at,
               i.account_login, i.status AS installation_status
        FROM github_repositories AS r
        JOIN github_installations AS i ON i.installation_id = r.installation_id
        WHERE r.is_selected = 1
        ORDER BY r.full_name ASC
        """
    ) as cursor:
        rows = [dict(row) for row in await cursor.fetchall()]

    async with tx.execute(
        "SELECT service, repository_id FROM github_service_mappings ORDER BY service ASC"
    ) as cursor:
        mapping_rows = await cursor.fetchall()

    services: dict[int, list[str]] = {}
    for mapping in mapping_rows:
        services.setdefault(int(mapping["repository_id"]), []).append(mapping["service"])

    for row in rows:
        mapped = services.get(int(row["repository_id"]), [])
        row["services"] = mapped
        # Retained for callers that predate multiple mappings; it is the first
        # service alphabetically, and `services` is the complete answer.
        row["service"] = mapped[0] if mapped else None
    return rows


async def get_installation(tx: aiosqlite.Connection, installation_id: int) -> dict[str, Any]:
    async with tx.execute(
        """
        SELECT i.installation_id, i.account_login, i.account_type, i.repository_selection,
               i.status, i.suspended_at, i.permissions_json, i.installed_at, i.updated_at,
               COALESCE(v.revision, 0) AS state_revision
        FROM github_installations AS i
        LEFT JOIN github_installation_state_versions AS v
            ON v.installation_id = i.installation_id
        WHERE i.installation_id = ?
        """,
        (installation_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise GitHubStoreError("GitHub installation was not found")
    record = dict(row)
    record["permissions"] = json.loads(record.pop("permissions_json"))
    return record


async def get_repository(tx: aiosqlite.Connection, repository_id: int) -> dict[str, Any]:
    async with tx.execute(
        """
        SELECT r.repository_id, r.installation_id, r.owner, r.name, r.full_name,
               r.default_branch, r.html_url, r.is_private, r.is_archived,
               r.is_selected, r.last_seen_commit_sha, i.status AS installation_status
        FROM github_repositories AS r
        JOIN github_installations AS i ON i.installation_id = r.installation_id
        WHERE r.repository_id = ?
        """,
        (repository_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise GitHubStoreError("repository binding was not found")
    return dict(row)


async def set_service_mapping(
    tx: aiosqlite.Connection, *, service: str, repository_id: int
) -> dict[str, Any]:
    """Map one monitored service to one selected, active GitHub repository."""

    service = _text(service, "service")
    if "/" in service or "," in service:
        raise GitHubStoreError("service contains unsupported characters")
    repository = await get_repository(tx, repository_id)
    if not repository["is_selected"] or repository["installation_status"] != "active":
        raise GitHubStoreError("repository is not selected in an active installation")
    await tx.execute(
        """
        INSERT INTO github_service_mappings (service, repository_id)
        VALUES (?, ?)
        ON CONFLICT(service) DO UPDATE SET
            repository_id = excluded.repository_id,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (service, repository_id),
    )
    return {"service": service, "repository_id": repository_id, "full_name": repository["full_name"]}


async def create_snapshot(
    tx: aiosqlite.Connection,
    *,
    repository_id: int,
    ref: str,
    commit_sha: str,
    tree_sha: str,
    tree_entries: Sequence[Mapping[str, Any]],
    tree_truncated: bool,
) -> dict[str, Any]:
    """Persist an immutable Git object inventory, never source file contents."""

    await require_active_selected_repository(tx, repository_id)
    if not commit_sha or not tree_sha:
        raise GitHubStoreError("commit_sha and tree_sha are required")
    snapshot_id = str(uuid4())
    files: list[tuple[str, str, str, str, int | None]] = []
    for entry in tree_entries:
        if entry.get("type") != "blob":
            continue
        path = _text(entry.get("path"), "tree entry path")
        blob_sha = _text(entry.get("sha"), "tree entry sha")
        mode = _text(entry.get("mode"), "tree entry mode")
        size = entry.get("size")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
            raise GitHubStoreError("tree entry size must be an integer or null")
        files.append((path, blob_sha, mode, "blob", size))

    await tx.execute(
        """
        INSERT OR IGNORE INTO github_snapshots (
            snapshot_id, repository_id, ref, commit_sha, tree_sha, file_count, tree_truncated
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            repository_id,
            _text(ref, "ref"),
            commit_sha,
            tree_sha,
            len(files),
            _bool(tree_truncated),
        ),
    )
    async with tx.execute("SELECT changes() AS changed") as cursor:
        inserted = bool((await cursor.fetchone())["changed"])
    if inserted:
        if files:
            await tx.executemany(
                """
                INSERT INTO github_snapshot_files (
                    snapshot_id, path, blob_sha, mode, object_type, size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(snapshot_id, *entry) for entry in files],
            )
    else:
        async with tx.execute(
            """
            SELECT snapshot_id FROM github_snapshots
            WHERE repository_id = ? AND commit_sha = ?
            """,
            (repository_id, commit_sha),
        ) as cursor:
            existing = await cursor.fetchone()
        if existing is None:
            raise RuntimeError("existing GitHub snapshot could not be loaded")
        snapshot_id = str(existing["snapshot_id"])

    await tx.execute(
        """
        UPDATE github_repositories
        SET last_seen_commit_sha = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE repository_id = ?
        """,
        (commit_sha, repository_id),
    )
    return await get_snapshot(tx, snapshot_id)


async def get_snapshot(tx: aiosqlite.Connection, snapshot_id: str) -> dict[str, Any]:
    async with tx.execute(
        """
        SELECT s.snapshot_id, s.repository_id, r.full_name, s.ref, s.commit_sha,
               s.tree_sha, s.file_count, s.tree_truncated, s.created_at
        FROM github_snapshots AS s
        JOIN github_repositories AS r ON r.repository_id = s.repository_id
        WHERE s.snapshot_id = ?
        """,
        (snapshot_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise GitHubStoreError("snapshot was not found")
    return dict(row)


async def list_snapshot_files(
    tx: aiosqlite.Connection, snapshot_id: str, *, limit: int = 500
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1_000))
    async with tx.execute(
        """
        SELECT path, blob_sha, mode, object_type, size_bytes
        FROM github_snapshot_files
        WHERE snapshot_id = ?
        ORDER BY path ASC
        LIMIT ?
        """,
        (snapshot_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_snapshot_files_by_paths(
    tx: aiosqlite.Connection,
    snapshot_id: str,
    *,
    paths: Sequence[str],
) -> list[dict[str, Any]]:
    """Load a small, explicit set of immutable snapshot file records.

    Patch preview must never enumerate an entire repository merely to inspect a
    handful of model-proposed targets.  Keeping this lookup bounded also makes
    it easy for callers to verify that every target is present in the pinned
    snapshot before any blob is fetched from GitHub.
    """

    snapshot_id = _text(snapshot_id, "snapshot_id")
    if isinstance(paths, (str, bytes)) or len(paths) > 100:
        raise GitHubStoreError("snapshot file paths must be a sequence of at most 100 entries")
    normalized_paths: list[str] = []
    seen_paths: set[str] = set()
    for path in paths:
        normalized = _text(path, "snapshot file path")
        if normalized in seen_paths:
            continue
        seen_paths.add(normalized)
        normalized_paths.append(normalized)
    if not normalized_paths:
        return []

    placeholders = ", ".join("?" for _ in normalized_paths)
    async with tx.execute(
        f"""
        SELECT path, blob_sha, mode, object_type, size_bytes
        FROM github_snapshot_files
        WHERE snapshot_id = ? AND path IN ({placeholders})
        ORDER BY path ASC
        """,
        (snapshot_id, *normalized_paths),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def list_snapshot_source_inventory(
    tx: aiosqlite.Connection,
    snapshot_id: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return a bounded, metadata-only blob inventory for source selection.

    A diagnosis ranks paths before it retrieves any blob content. The snapshot
    creation route has already bounded the total inventory, but callers must
    still provide an explicit positive read bound so a later configuration
    change cannot turn an optional diagnosis into an unbounded query.
    """

    snapshot_id = _text(snapshot_id, "snapshot_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20_000:
        raise GitHubStoreError("snapshot source inventory limit must be an integer from 1 through 20000")
    async with tx.execute(
        """
        SELECT path, blob_sha, mode, object_type, size_bytes
        FROM github_snapshot_files
        WHERE snapshot_id = ? AND object_type = 'blob'
        ORDER BY path ASC
        LIMIT ?
        """,
        (snapshot_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]
