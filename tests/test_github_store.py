"""Persistence tests for Phase 1's read-only GitHub boundary."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from src.github_integration.store import (
    GitHubConnectionStateError,
    GitHubStoreError,
    create_snapshot,
    get_installation,
    get_repository,
    get_snapshot,
    list_repositories,
    list_snapshot_files,
    record_webhook_delivery,
    replace_installation_repositories,
    set_service_mapping,
    sync_installation_repositories_from_webhook,
    upsert_installation_from_webhook,
    upsert_repository,
)


SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


@pytest_asyncio.fixture
async def db_conn() -> aiosqlite.Connection:
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield connection
    await connection.close()


def _repository(repository_id: int = 8123) -> dict:
    return {
        "id": repository_id,
        "name": "checkout-api",
        "full_name": "acme/checkout-api",
        "owner": {"login": "acme"},
        "default_branch": "main",
        "html_url": "https://github.com/acme/checkout-api",
        "private": True,
        "archived": False,
    }


def _installation_payload(action: str = "created") -> dict:
    return {
        "action": action,
        "installation": {
            "id": 9988,
            "account": {"login": "acme", "type": "Organization"},
            "repository_selection": "selected",
            "permissions": {"metadata": "read", "contents": "read"},
            "suspended_at": None,
            "updated_at": "2026-09-04T10:00:00Z",
        },
        "repositories": [_repository()],
    }


async def _install(db_conn: aiosqlite.Connection, payload: dict, action: str = "created") -> int:
    installation_id, applied = await upsert_installation_from_webhook(db_conn, payload, action)
    assert applied
    return installation_id


@pytest.mark.asyncio
async def test_installation_and_selected_repository_are_persisted(db_conn) -> None:
    payload = _installation_payload()
    installation_id = await _install(db_conn, payload)
    repository_id = await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )

    repository = await get_repository(db_conn, repository_id)
    assert repository["full_name"] == "acme/checkout-api"
    assert repository["default_branch"] == "main"
    assert repository["installation_status"] == "active"
    assert repository["is_selected"] == 1


@pytest.mark.asyncio
async def test_compact_github_webhook_repository_and_unix_timestamp_are_accepted(db_conn) -> None:
    """GitHub installation fixtures omit owner/default_branch and use epoch seconds."""

    payload = _installation_payload()
    payload["installation"]["updated_at"] = 1_557_933_591
    compact_repository = {
        "id": 8123,
        "name": "checkout-api",
        "full_name": "acme/checkout-api",
        "private": True,
    }
    payload["repositories"] = [compact_repository]

    installation_id = await _install(db_conn, payload)
    repository_id = await upsert_repository(
        db_conn, installation_id=installation_id, repository=compact_repository
    )

    repository = await get_repository(db_conn, repository_id)
    assert repository["owner"] == "acme"
    assert repository["default_branch"] == ""


@pytest.mark.asyncio
async def test_broad_github_installation_is_stored_but_not_usable(db_conn) -> None:
    payload = _installation_payload()
    payload["installation"]["repository_selection"] = "all"
    installation_id = await _install(db_conn, payload)
    repository_id = await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )

    repository = await get_repository(db_conn, repository_id)
    assert repository["installation_status"] == "repository_selection_required"
    assert repository["is_selected"] == 1
    with pytest.raises(GitHubStoreError, match="active installation"):
        await set_service_mapping(db_conn, service="checkout-api", repository_id=repository_id)


@pytest.mark.asyncio
async def test_write_permission_installation_is_never_usable(db_conn) -> None:
    payload = _installation_payload()
    payload["installation"]["permissions"] = {"metadata": "read", "contents": "write"}
    installation_id = await _install(db_conn, payload)
    repository_id = await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )

    assert (await get_repository(db_conn, repository_id))["installation_status"] == "permission_misconfigured"


@pytest.mark.asyncio
async def test_installation_repositories_webhook_updates_selection(db_conn) -> None:
    payload = _installation_payload()
    installation_id = await _install(db_conn, payload)
    await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )
    event = {
        "action": "removed",
        "installation": payload["installation"],
        "repository_selection": "selected",
        "repositories_added": [],
        "repositories_removed": [payload["repositories"][0]],
    }

    await sync_installation_repositories_from_webhook(db_conn, event)

    assert await list_repositories(db_conn) == []
    assert (await get_repository(db_conn, 8123))["is_selected"] == 0


@pytest.mark.asyncio
async def test_repository_selection_change_to_all_is_disabled_before_sync(db_conn) -> None:
    payload = _installation_payload()
    installation_id = await _install(db_conn, payload)
    await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )
    all_repositories_installation = dict(payload["installation"])
    all_repositories_installation["repository_selection"] = "all"

    _, installation_status, _ = await sync_installation_repositories_from_webhook(
        db_conn,
        {
            "action": "added",
            "installation": all_repositories_installation,
            "repository_selection": "all",
            "repositories_added": [_repository(9123)],
            "repositories_removed": [],
        },
    )

    assert installation_status == "repository_selection_required"
    assert (await get_installation(db_conn, installation_id))["repository_selection"] == "all"
    assert await list_repositories(db_conn) == []


@pytest.mark.asyncio
async def test_out_of_order_repository_add_cannot_restore_a_newer_removal(db_conn) -> None:
    payload = _installation_payload()
    payload["installation"]["updated_at"] = "2026-09-04T10:00:00Z"
    installation_id = await _install(db_conn, payload)
    await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )

    removed = {
        "action": "removed",
        "installation": deepcopy(payload["installation"]),
        "repository_selection": "selected",
        "repositories_added": [],
        "repositories_removed": [payload["repositories"][0]],
    }
    removed["installation"]["updated_at"] = "2026-09-04T10:00:30Z"
    _, _, removal_applied = await sync_installation_repositories_from_webhook(db_conn, removed)

    stale_added = {
        "action": "added",
        "installation": deepcopy(payload["installation"]),
        "repository_selection": "selected",
        "repositories_added": [payload["repositories"][0]],
        "repositories_removed": [],
    }
    stale_added["installation"]["updated_at"] = "2026-09-04T10:00:20Z"
    _, _, addition_applied = await sync_installation_repositories_from_webhook(db_conn, stale_added)

    assert removal_applied is True
    assert addition_applied is False
    assert (await get_repository(db_conn, 8123))["is_selected"] == 0


@pytest.mark.asyncio
async def test_out_of_order_created_event_cannot_resurrect_a_deleted_installation(db_conn) -> None:
    created = _installation_payload()
    created["installation"]["updated_at"] = "2026-09-04T10:00:00Z"
    installation_id = await _install(db_conn, created)
    await upsert_repository(
        db_conn, installation_id=installation_id, repository=created["repositories"][0]
    )

    deleted = deepcopy(created)
    deleted["installation"]["updated_at"] = "2026-09-04T10:01:00Z"
    await _install(db_conn, deleted, "deleted")

    stale_installation_id, applied = await upsert_installation_from_webhook(
        db_conn, created, "created"
    )

    assert stale_installation_id == installation_id
    assert applied is False
    assert (await get_installation(db_conn, installation_id))["status"] == "deleted"
    assert (await get_repository(db_conn, 8123))["is_selected"] == 0


@pytest.mark.asyncio
async def test_repository_webhook_can_create_a_new_installation_before_created_event(db_conn) -> None:
    payload = _installation_payload()
    payload["installation"]["updated_at"] = "2026-09-04T10:01:00Z"
    event = {
        "action": "added",
        "installation": payload["installation"],
        "repository_selection": "selected",
        "repositories_added": [payload["repositories"][0]],
        "repositories_removed": [],
    }

    installation_id, status, applied = await sync_installation_repositories_from_webhook(
        db_conn, event
    )

    assert (installation_id, status, applied) == (9988, "active", True)
    assert (await get_repository(db_conn, 8123))["is_selected"] == 1


@pytest.mark.asyncio
async def test_stale_sync_cannot_reselect_a_revoked_installation(db_conn) -> None:
    """A webhook processed during remote sync must win over stale API data."""

    payload = _installation_payload()
    installation_id = await _install(db_conn, payload)
    await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )
    expected_state_revision = (await get_installation(db_conn, installation_id))["state_revision"]
    await _install(db_conn, payload, "deleted")

    with pytest.raises(GitHubConnectionStateError, match="no longer"):
        await replace_installation_repositories(
            db_conn,
            installation_id=installation_id,
            expected_state_revision=expected_state_revision,
            repositories=[_repository()],
        )

    assert (await get_repository(db_conn, 8123))["is_selected"] == 0


@pytest.mark.asyncio
async def test_service_mapping_requires_active_selected_repository(db_conn) -> None:
    payload = _installation_payload()
    installation_id = await _install(db_conn, payload)
    repository_id = await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )

    mapping = await set_service_mapping(
        db_conn, service="checkout-api", repository_id=repository_id
    )

    assert mapping == {
        "service": "checkout-api",
        "repository_id": repository_id,
        "full_name": "acme/checkout-api",
    }
    assert (await list_repositories(db_conn))[0]["service"] == "checkout-api"


@pytest.mark.asyncio
async def test_snapshot_is_pinned_to_git_objects_without_storing_content(db_conn) -> None:
    payload = _installation_payload()
    installation_id = await _install(db_conn, payload)
    repository_id = await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )
    entries = [
        {"path": "src/main.py", "mode": "100644", "type": "blob", "sha": "blob-main", "size": 37},
        {"path": "src", "mode": "040000", "type": "tree", "sha": "tree-src"},
    ]

    snapshot = await create_snapshot(
        db_conn,
        repository_id=repository_id,
        ref="main",
        commit_sha="commit-123",
        tree_sha="tree-123",
        tree_entries=entries,
        tree_truncated=False,
    )

    assert snapshot["commit_sha"] == "commit-123"
    assert snapshot["tree_sha"] == "tree-123"
    assert snapshot["file_count"] == 1
    assert await list_snapshot_files(db_conn, snapshot["snapshot_id"]) == [
        {
            "path": "src/main.py",
            "blob_sha": "blob-main",
            "mode": "100644",
            "object_type": "blob",
            "size_bytes": 37,
        }
    ]
    async with db_conn.execute("SELECT COUNT(*) AS count FROM github_snapshot_files") as cursor:
        assert (await cursor.fetchone())["count"] == 1


@pytest.mark.asyncio
async def test_stale_snapshot_cannot_persist_after_repository_removal(db_conn) -> None:
    """Selection is rechecked under the writer lock after remote GitHub reads."""

    payload = _installation_payload()
    installation_id = await _install(db_conn, payload)
    repository_id = await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )
    await sync_installation_repositories_from_webhook(
        db_conn,
        {
            "action": "removed",
            "installation": payload["installation"],
            "repository_selection": "selected",
            "repositories_added": [],
            "repositories_removed": [payload["repositories"][0]],
        },
    )

    with pytest.raises(GitHubConnectionStateError, match="no longer"):
        await create_snapshot(
            db_conn,
            repository_id=repository_id,
            ref="main",
            commit_sha="commit-after-removal",
            tree_sha="tree-after-removal",
            tree_entries=[],
            tree_truncated=False,
        )


@pytest.mark.asyncio
async def test_same_repository_commit_reuses_immutable_snapshot(db_conn) -> None:
    payload = _installation_payload()
    installation_id = await _install(db_conn, payload)
    repository_id = await upsert_repository(
        db_conn, installation_id=installation_id, repository=payload["repositories"][0]
    )
    kwargs = {
        "repository_id": repository_id,
        "ref": "main",
        "commit_sha": "same-commit",
        "tree_sha": "same-tree",
        "tree_entries": [],
        "tree_truncated": False,
    }
    first = await create_snapshot(db_conn, **kwargs)
    second = await create_snapshot(db_conn, **kwargs)

    assert first["snapshot_id"] == second["snapshot_id"]
    assert (await get_snapshot(db_conn, first["snapshot_id"]))["commit_sha"] == "same-commit"


@pytest.mark.asyncio
async def test_webhook_delivery_ids_are_idempotent(db_conn) -> None:
    first = await record_webhook_delivery(
        db_conn,
        delivery_id="delivery-1",
        event_type="installation",
        action="created",
        installation_id=9988,
        payload_sha256="abc123",
    )
    replay = await record_webhook_delivery(
        db_conn,
        delivery_id="delivery-1",
        event_type="installation",
        action="created",
        installation_id=9988,
        payload_sha256="abc123",
    )

    assert first is True
    assert replay is False
