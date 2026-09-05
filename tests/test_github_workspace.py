"""Focused tests for the local-only proposed-patch workspace."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.github_integration.workspace import (
    LocalPatchWorkspace,
    PatchConflictError,
    PatchValidationError,
    WorkspaceLimits,
)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_applies_structured_patch_locally_and_returns_reviewable_diff(tmp_path: Path) -> None:
    base_files = {
        "src/app.py": "def greet(name):\n    return f'Hello {name}'\n",
        "README.md": "# PulseGraph\n",
        "obsolete.txt": "remove me\n",
    }
    proposal = {
        "patch_id": "incident-1042-null-guard",
        "summary": "Guard greeting against a missing name.",
        "rationale": "The model traced the repeated alert group to a null input.",
        "changes": [
            {
                "action": "update",
                "path": "src/app.py",
                "content": "def greet(name):\n    return f'Hello {name or \\\"anonymous\\\"}'\n",
                "expected_sha256": _sha256(base_files["src/app.py"]),
                "explanation": "Use a local fallback for optional input.",
            },
            {
                "action": "create",
                "path": "tests/test_app.py",
                "content": "from src.app import greet\n\n\ndef test_missing_name_is_safe():\n    assert greet(None) == 'Hello anonymous'\n",
                "explanation": "Cover the reported null-input path.",
            },
            {
                "action": "delete",
                "path": "obsolete.txt",
                "expected_sha256": _sha256(base_files["obsolete.txt"]),
                "explanation": "The old placeholder is no longer used.",
            },
        ],
    }

    with LocalPatchWorkspace(base_files, temp_parent=tmp_path) as workspace:
        root = workspace.root
        review = workspace.apply(proposal)

        assert (root / "src" / "app.py").read_text(encoding="utf-8") == proposal["changes"][0]["content"]
        assert (root / "tests" / "test_app.py").read_text(encoding="utf-8") == proposal["changes"][1]["content"]
        assert not (root / "obsolete.txt").exists()
        assert not (root / ".git").exists()

        assert review.patch_id == proposal["patch_id"]
        assert [change.action.value for change in review.changed_files] == ["update", "create", "delete"]
        assert review.changed_files[0].before_sha256 == _sha256(base_files["src/app.py"])
        assert review.changed_files[1].before_sha256 is None
        assert review.changed_files[2].after_sha256 is None
        assert "--- a/src/app.py" in review.unified_diff
        assert "+++ b/src/app.py" in review.unified_diff
        assert "--- /dev/null" in review.unified_diff
        assert "+++ b/tests/test_app.py" in review.unified_diff
        assert "--- a/obsolete.txt" in review.unified_diff
        assert "+++ /dev/null" in review.unified_diff
        assert review.metadata["execution_scope"] == "local_ephemeral_workspace_only"
        assert review.metadata["human_review_required"] is True
        assert review.metadata["git_commands_executed"] is False
        assert review.metadata["network_accessed"] is False
        assert review.metadata["source_contents_persisted"] is False
        assert review.metadata["base_file_count"] == 3
        assert review.metadata["result_file_count"] == 3

    assert not root.exists()


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.py",
        "src/../outside.py",
        "/absolute.py",
        "C:/outside.py",
        "src\\outside.py",
        "NUL.txt",
        "ambiguous.",
    ],
)
def test_rejects_traversal_and_ambiguous_paths_without_writing_outside_workspace(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    outside_file = tmp_path / "outside.py"
    proposal = {
        "patch_id": "unsafe-path",
        "summary": "This must be rejected.",
        "changes": [{"action": "create", "path": unsafe_path, "content": "unsafe\n"}],
    }

    with LocalPatchWorkspace({"safe.py": "safe\n"}, temp_parent=tmp_path) as workspace:
        with pytest.raises(PatchValidationError):
            workspace.apply(proposal)
        assert (workspace.root / "safe.py").read_text(encoding="utf-8") == "safe\n"
        assert not outside_file.exists()


def test_size_file_count_and_patch_limits_reject_proposal_without_partial_application(tmp_path: Path) -> None:
    limits = WorkspaceLimits(
        max_file_count=2,
        max_changes=2,
        max_file_bytes=8,
        max_total_bytes=12,
        max_patch_bytes=8,
        max_diff_bytes=100,
        max_path_length=100,
    )
    proposal = {
        "patch_id": "too-large",
        "summary": "Try to exceed the bounded workspace.",
        "changes": [
            {"action": "update", "path": "safe.py", "content": "nine-byte\n"},
            {"action": "create", "path": "new.py", "content": "new\n"},
        ],
    }

    with LocalPatchWorkspace({"safe.py": "old\n"}, limits=limits, temp_parent=tmp_path) as workspace:
        with pytest.raises(PatchValidationError, match="max_file_bytes"):
            workspace.apply(proposal)
        assert (workspace.root / "safe.py").read_text(encoding="utf-8") == "old\n"
        assert not (workspace.root / "new.py").exists()

        too_many_files = {
            "patch_id": "too-many-files",
            "summary": "Exceed the file-count bound.",
            "changes": [
                {"action": "create", "path": "new.py", "content": "new\n"},
                {"action": "create", "path": "two.py", "content": "two\n"},
            ],
        }
        with pytest.raises(PatchValidationError, match="max_file_count"):
            workspace.apply(too_many_files)
        assert not (workspace.root / "new.py").exists()
        assert not (workspace.root / "two.py").exists()

        too_large_patch = {
            "patch_id": "too-large-patch",
            "summary": "Exceed the aggregate patch bound.",
            "changes": [
                {"action": "update", "path": "safe.py", "content": "1234567"},
                {"action": "create", "path": "new.py", "content": "x\n"},
            ],
        }
        with pytest.raises(PatchValidationError, match="max_patch_bytes"):
            workspace.apply(too_large_patch)
        assert (workspace.root / "safe.py").read_text(encoding="utf-8") == "old\n"
        assert not (workspace.root / "new.py").exists()


def test_hash_conflicts_duplicate_changes_and_unknown_schema_are_rejected(tmp_path: Path) -> None:
    with LocalPatchWorkspace({"src/app.py": "before\n"}, temp_parent=tmp_path) as workspace:
        conflict = {
            "patch_id": "wrong-base",
            "summary": "Use a stale source version.",
            "changes": [
                {
                    "action": "update",
                    "path": "src/app.py",
                    "content": "after\n",
                    "expected_sha256": "0" * 64,
                }
            ],
        }
        with pytest.raises(PatchConflictError, match="source hash"):
            workspace.apply(conflict)
        assert (workspace.root / "src" / "app.py").read_text(encoding="utf-8") == "before\n"

        duplicate = {
            "patch_id": "duplicate",
            "summary": "Change one file twice.",
            "changes": [
                {"action": "update", "path": "src/app.py", "content": "after\n"},
                {"action": "delete", "path": "src/app.py"},
            ],
        }
        with pytest.raises(PatchValidationError, match="same path"):
            workspace.apply(duplicate)

        unsupported_field = {
            "patch_id": "invalid-schema",
            "summary": "Reject non-contract output.",
            "changes": [{"action": "update", "path": "src/app.py", "content": "after\n"}],
            "push_to_github": True,
        }
        with pytest.raises(PatchValidationError, match="unsupported field"):
            workspace.apply(unsupported_field)


def test_can_safely_replace_a_file_with_a_directory_and_the_reverse(tmp_path: Path) -> None:
    base_files = {
        "module": "legacy module file\n",
        "package/legacy.py": "legacy package content\n",
    }
    proposal = {
        "patch_id": "source-layout-fix",
        "summary": "Replace obsolete source-layout placeholders.",
        "changes": [
            {"action": "delete", "path": "module"},
            {"action": "create", "path": "module/app.py", "content": "print('new module')\n"},
            {"action": "delete", "path": "package/legacy.py"},
            {"action": "create", "path": "package", "content": "package marker\n"},
        ],
    }

    with LocalPatchWorkspace(base_files, temp_parent=tmp_path) as workspace:
        review = workspace.apply(proposal)

        assert (workspace.root / "module").is_dir()
        assert (workspace.root / "module" / "app.py").read_text(encoding="utf-8") == "print('new module')\n"
        assert (workspace.root / "package").is_file()
        assert (workspace.root / "package").read_text(encoding="utf-8") == "package marker\n"
        assert review.metadata["result_file_count"] == 2


def test_workspace_is_cleaned_up_after_an_exception(tmp_path: Path) -> None:
    workspace = LocalPatchWorkspace({"safe.py": "safe\n"}, temp_parent=tmp_path)
    root = workspace.root

    with pytest.raises(RuntimeError):
        with workspace:
            raise RuntimeError("caller failed after creating a proposal")

    assert not root.exists()
