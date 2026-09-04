"""Local-only workspace support for proposed GitHub source patches.

This module deliberately knows nothing about GitHub credentials, network
clients, or git.  A caller supplies already-fetched textual source files and
a structured proposal, then receives a reviewable unified diff after the
proposal is applied in a private temporary directory.  The directory is
removed when the context manager exits.

It is intentionally a small safety boundary for the Phase 4 "propose a fix"
workflow: the result is for a human to review and apply in their own GitHub
repository.  This component cannot push, pull, create a branch, or execute a
command.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import difflib
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import uuid4


class PatchWorkspaceError(Exception):
    """Base error for an invalid or unsafe local patch workspace operation."""


class PatchValidationError(PatchWorkspaceError):
    """Raised when a proposal, path, or size limit is invalid."""


class PatchConflictError(PatchWorkspaceError):
    """Raised when a proposal no longer matches the workspace base state."""


class PatchWorkspaceClosedError(PatchWorkspaceError):
    """Raised when an operation is attempted after workspace cleanup."""


class ChangeAction(str, Enum):
    """Supported operations in a structured proposed patch."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class WorkspaceLimits:
    """Hard bounds for an ephemeral patch workspace.

    These defaults keep a model-generated proposal small enough for human
    review while allowing ordinary application fixes.  Callers can choose
    tighter bounds for a particular deployment.
    """

    max_file_count: int = 500
    max_changes: int = 100
    max_file_bytes: int = 512 * 1024
    max_total_bytes: int = 5 * 1024 * 1024
    max_patch_bytes: int = 1 * 1024 * 1024
    max_diff_bytes: int = 2 * 1024 * 1024
    max_path_length: int = 240

    def __post_init__(self) -> None:
        for field_name in (
            "max_file_count",
            "max_changes",
            "max_file_bytes",
            "max_total_bytes",
            "max_patch_bytes",
            "max_diff_bytes",
            "max_path_length",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ProposedFileChange:
    """One textual file operation proposed by an analysis model.

    ``expected_sha256`` is optional for convenience, but callers should pass
    it for updates and deletes created from a pinned source snapshot.  It makes
    the proposal fail closed if the local base differs from the model's input.
    """

    path: str
    action: ChangeAction | str
    content: str | None = None
    expected_sha256: str | None = None
    explanation: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ProposedFileChange":
        _require_mapping(payload, "change")
        allowed_keys = {"path", "action", "content", "expected_sha256", "explanation"}
        _reject_unknown_keys(payload, allowed_keys, "change")
        if "path" not in payload or "action" not in payload:
            raise PatchValidationError("each change requires path and action")
        return cls(
            path=payload["path"],
            action=payload["action"],
            content=payload.get("content"),
            expected_sha256=payload.get("expected_sha256"),
            explanation=payload.get("explanation"),
        )


@dataclass(frozen=True, slots=True)
class ProposedPatch:
    """A narrow, JSON-compatible proposal that can be reviewed locally.

    The accepted mapping shape is intentionally explicit::

        {
          "patch_id": "incident-123-fix",
          "summary": "Guard against a missing config value",
          "rationale": "The exception happens before the fallback is used.",
          "changes": [
            {"action": "update", "path": "src/app.py", "content": "...",
             "expected_sha256": "<optional 64-character SHA-256>"}
          ]
        }
    """

    patch_id: str
    summary: str
    changes: tuple[ProposedFileChange, ...]
    rationale: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ProposedPatch":
        _require_mapping(payload, "proposal")
        allowed_keys = {"patch_id", "summary", "rationale", "changes"}
        _reject_unknown_keys(payload, allowed_keys, "proposal")
        missing = {"patch_id", "summary", "changes"}.difference(payload)
        if missing:
            names = ", ".join(sorted(missing))
            raise PatchValidationError(f"proposal is missing required field(s): {names}")
        raw_changes = payload["changes"]
        if not isinstance(raw_changes, Sequence) or isinstance(raw_changes, (str, bytes, bytearray)):
            raise PatchValidationError("proposal changes must be a list")
        return cls(
            patch_id=payload["patch_id"],
            summary=payload["summary"],
            rationale=payload.get("rationale"),
            changes=tuple(ProposedFileChange.from_mapping(change) for change in raw_changes),
        )


@dataclass(frozen=True, slots=True)
class ReviewedFileChange:
    """Review data for one successfully planned and applied file operation."""

    path: str
    action: ChangeAction
    before_sha256: str | None
    after_sha256: str | None
    before_bytes: int
    after_bytes: int
    explanation: str | None


@dataclass(frozen=True, slots=True)
class PatchReview:
    """An immutable local review artifact; it contains no GitHub credentials."""

    patch_id: str
    summary: str
    rationale: str | None
    changed_files: tuple[ReviewedFileChange, ...]
    unified_diff: str
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _PlannedChange:
    change: ProposedFileChange
    action: ChangeAction
    path: str
    before: str | None
    after: str | None


class LocalPatchWorkspace:
    """A temporary, textual source workspace for reviewing a proposed patch.

    ``base_files`` maps repository-relative POSIX paths to UTF-8 text (or UTF-8
    bytes).  It never writes outside the directory created with ``mkdtemp``.
    Use it only as a context manager so cleanup is guaranteed even when a
    proposal is rejected or a caller raises an exception.
    """

    def __init__(
        self,
        base_files: Mapping[str, str | bytes],
        *,
        limits: WorkspaceLimits | None = None,
        temp_parent: str | Path | None = None,
    ) -> None:
        self._limits = limits or WorkspaceLimits()
        self._closed = False
        self._workspace_id = uuid4().hex
        self._container = Path(
            tempfile.mkdtemp(
                prefix="pulsegraph-patch-",
                dir=str(temp_parent) if temp_parent is not None else None,
            )
        ).resolve()
        self._root = self._container / "workspace"
        try:
            # On POSIX this prevents other local users from inspecting a
            # proposal.  It is harmless on Windows, where the ACL controls it.
            os.chmod(self._container, 0o700)
            self._root.mkdir(mode=0o700)
            normalized_base = self._normalize_base_files(base_files)
            self._write_result_files({}, normalized_base)
        except Exception:
            self.cleanup()
            raise

    def __enter__(self) -> "LocalPatchWorkspace":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.cleanup()

    @property
    def root(self) -> Path:
        """The private temporary root, available only until cleanup."""

        self._ensure_open()
        return self._root

    @property
    def workspace_id(self) -> str:
        """A non-secret identifier for review/audit correlation."""

        return self._workspace_id

    def cleanup(self) -> None:
        """Remove only the temporary directory this instance created."""

        if self._closed:
            return
        self._closed = True
        # ``_container`` is always a unique mkdtemp result created above, never
        # a caller-supplied broad directory.  Do not resolve a replacement
        # symbolic link before deleting: unlink that link rather than following
        # it to a potentially unrelated directory.
        container = self._container
        if not container.name.startswith("pulsegraph-patch-"):
            return
        try:
            if not container.exists() and not container.is_symlink():
                return
            if _is_link_or_reparse_point(container):
                container.unlink()
                return
            shutil.rmtree(container)
        except OSError as exc:
            # Never silently claim that a workspace was cleaned up when the
            # operating system rejected the removal.  The caller can surface
            # this operational failure instead of leaving source in temp space
            # without knowing it.
            raise PatchWorkspaceError("unable to clean up temporary patch workspace") from exc

    def apply(self, proposal: ProposedPatch | Mapping[str, Any]) -> PatchReview:
        """Validate, apply, and describe a proposal entirely within this root.

        Validation and unified-diff construction happen before any target file
        is changed.  Therefore rejected proposals leave the workspace as it
        was, which gives callers an all-or-nothing review boundary.
        """

        self._ensure_open()
        normalized_proposal = self._coerce_proposal(proposal)
        current_files = self._read_workspace_files()
        planned_changes, resulting_files = self._plan_changes(normalized_proposal, current_files)
        unified_diff = self._build_unified_diff(planned_changes)
        diff_bytes = _utf8_bytes(unified_diff, "generated unified diff")
        if len(diff_bytes) > self._limits.max_diff_bytes:
            raise PatchValidationError(
                f"generated unified diff exceeds max_diff_bytes ({self._limits.max_diff_bytes})"
            )

        self._write_result_files(current_files, resulting_files)
        reviewed_changes = tuple(
            ReviewedFileChange(
                path=item.path,
                action=item.action,
                before_sha256=_sha256(item.before) if item.before is not None else None,
                after_sha256=_sha256(item.after) if item.after is not None else None,
                before_bytes=len(_utf8_bytes(item.before, item.path)) if item.before is not None else 0,
                after_bytes=len(_utf8_bytes(item.after, item.path)) if item.after is not None else 0,
                explanation=item.change.explanation,
            )
            for item in planned_changes
        )
        metadata = MappingProxyType(
            {
                "workspace_id": self._workspace_id,
                "execution_scope": "local_ephemeral_workspace_only",
                "human_review_required": True,
                "git_commands_executed": False,
                "network_accessed": False,
                "source_contents_persisted": False,
                "base_file_count": len(current_files),
                "result_file_count": len(resulting_files),
                "base_total_bytes": _total_bytes(current_files),
                "result_total_bytes": _total_bytes(resulting_files),
                "changed_file_count": len(reviewed_changes),
                "diff_bytes": len(diff_bytes),
                "workspace_cleanup": "on_context_exit_or_cleanup",
            }
        )
        return PatchReview(
            patch_id=normalized_proposal.patch_id,
            summary=normalized_proposal.summary,
            rationale=normalized_proposal.rationale,
            changed_files=reviewed_changes,
            unified_diff=unified_diff,
            metadata=metadata,
        )

    def _normalize_base_files(self, base_files: Mapping[str, str | bytes]) -> dict[str, str]:
        if not isinstance(base_files, Mapping):
            raise PatchValidationError("base_files must be a mapping of paths to UTF-8 text")
        normalized: dict[str, str] = {}
        for raw_path, raw_content in base_files.items():
            path = self._validate_path(raw_path)
            if path in normalized:
                raise PatchValidationError(f"duplicate base file path: {path}")
            normalized[path] = _coerce_text(raw_content, f"base file {path}")
        self._validate_file_set(normalized, context="base files")
        return normalized

    def _coerce_proposal(self, proposal: ProposedPatch | Mapping[str, Any]) -> ProposedPatch:
        if isinstance(proposal, ProposedPatch):
            return proposal
        if isinstance(proposal, Mapping):
            return ProposedPatch.from_mapping(proposal)
        raise PatchValidationError("proposal must be a ProposedPatch or JSON-compatible mapping")

    def _plan_changes(
        self,
        proposal: ProposedPatch,
        current_files: Mapping[str, str],
    ) -> tuple[tuple[_PlannedChange, ...], dict[str, str]]:
        _validate_metadata_text(proposal.patch_id, "patch_id", maximum=128)
        _validate_metadata_text(proposal.summary, "summary", maximum=2_000)
        if proposal.rationale is not None:
            _validate_metadata_text(proposal.rationale, "rationale", maximum=8_000)
        if not proposal.changes:
            raise PatchValidationError("proposal must contain at least one change")
        if len(proposal.changes) > self._limits.max_changes:
            raise PatchValidationError(
                f"proposal has more than max_changes ({self._limits.max_changes})"
            )

        result = dict(current_files)
        planned: list[_PlannedChange] = []
        seen_paths: set[str] = set()
        patch_content_bytes = 0
        for change in proposal.changes:
            if not isinstance(change, ProposedFileChange):
                raise PatchValidationError("proposal changes must be ProposedFileChange values")
            path = self._validate_path(change.path)
            if path in seen_paths:
                raise PatchValidationError(f"proposal changes the same path more than once: {path}")
            seen_paths.add(path)
            action = self._coerce_action(change.action)
            before = result.get(path)
            expected_sha256 = _normalize_sha256(change.expected_sha256)
            if change.explanation is not None:
                _validate_metadata_text(change.explanation, f"explanation for {path}", maximum=4_000)

            if action is ChangeAction.CREATE:
                if before is not None:
                    raise PatchConflictError(f"cannot create existing file: {path}")
                if expected_sha256 is not None:
                    raise PatchValidationError("create changes cannot set expected_sha256")
                self._ensure_no_file_directory_collision(path, result)
                after = self._require_change_content(change, path)
                result[path] = after
            elif action is ChangeAction.UPDATE:
                if before is None:
                    raise PatchConflictError(f"cannot update missing file: {path}")
                self._verify_expected_sha256(path, before, expected_sha256)
                after = self._require_change_content(change, path)
                if after == before:
                    raise PatchValidationError(f"update does not change file contents: {path}")
                result[path] = after
            else:
                if before is None:
                    raise PatchConflictError(f"cannot delete missing file: {path}")
                self._verify_expected_sha256(path, before, expected_sha256)
                if change.content is not None:
                    raise PatchValidationError("delete changes must not include content")
                after = None
                del result[path]

            if after is not None:
                patch_content_bytes += len(_utf8_bytes(after, f"proposal content for {path}"))
            if patch_content_bytes > self._limits.max_patch_bytes:
                raise PatchValidationError(
                    f"proposal content exceeds max_patch_bytes ({self._limits.max_patch_bytes})"
                )
            planned.append(_PlannedChange(change, action, path, before, after))

        self._validate_file_set(result, context="resulting files")
        return tuple(planned), result

    def _validate_file_set(self, files: Mapping[str, str], *, context: str) -> None:
        if len(files) > self._limits.max_file_count:
            raise PatchValidationError(
                f"{context} exceed max_file_count ({self._limits.max_file_count})"
            )
        total_bytes = 0
        for path, content in files.items():
            self._validate_path(path)
            size = len(_utf8_bytes(content, f"{context} content for {path}"))
            if size > self._limits.max_file_bytes:
                raise PatchValidationError(
                    f"{context} file {path} exceeds max_file_bytes ({self._limits.max_file_bytes})"
                )
            total_bytes += size
            if total_bytes > self._limits.max_total_bytes:
                raise PatchValidationError(
                    f"{context} exceed max_total_bytes ({self._limits.max_total_bytes})"
                )
        self._validate_no_file_directory_collisions(files)

    def _validate_path(self, raw_path: object) -> str:
        if not isinstance(raw_path, str):
            raise PatchValidationError("file path must be a string")
        if not raw_path or len(raw_path) > self._limits.max_path_length:
            raise PatchValidationError("file path is empty or exceeds max_path_length")
        if len(_utf8_bytes(raw_path, "file path")) > self._limits.max_path_length:
            raise PatchValidationError("file path UTF-8 length exceeds max_path_length")
        if "\x00" in raw_path or "\\" in raw_path:
            raise PatchValidationError("file path must be a NUL-free, POSIX-style relative path")
        if any(ord(character) < 32 for character in raw_path):
            raise PatchValidationError("file path cannot contain control characters")
        if raw_path.startswith("/") or PurePosixPath(raw_path).is_absolute() or PureWindowsPath(raw_path).is_absolute():
            raise PatchValidationError("absolute file paths are not allowed")
        parts = raw_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise PatchValidationError("file path cannot contain empty, dot, or parent segments")
        if any(part.endswith((".", " ")) for part in parts):
            raise PatchValidationError("file path segments cannot end with a dot or space")
        if any(":" in part for part in parts):
            raise PatchValidationError("file path cannot contain a colon")
        if any(_is_windows_reserved_name(part) for part in parts):
            raise PatchValidationError("file path uses a Windows-reserved filename")
        return raw_path

    def _coerce_action(self, raw_action: ChangeAction | str) -> ChangeAction:
        try:
            return ChangeAction(raw_action)
        except (TypeError, ValueError) as exc:
            values = ", ".join(action.value for action in ChangeAction)
            raise PatchValidationError(f"change action must be one of: {values}") from exc

    def _require_change_content(self, change: ProposedFileChange, path: str) -> str:
        if not isinstance(change.content, str):
            raise PatchValidationError(f"{change.action} change requires string content: {path}")
        if len(_utf8_bytes(change.content, f"proposal content for {path}")) > self._limits.max_file_bytes:
            raise PatchValidationError(
                f"proposal file {path} exceeds max_file_bytes ({self._limits.max_file_bytes})"
            )
        return change.content

    @staticmethod
    def _verify_expected_sha256(path: str, before: str, expected_sha256: str | None) -> None:
        if expected_sha256 is not None and _sha256(before) != expected_sha256:
            raise PatchConflictError(f"source hash no longer matches expected_sha256 for {path}")

    @staticmethod
    def _ensure_no_file_directory_collision(path: str, files: Mapping[str, str]) -> None:
        parts = path.split("/")
        ancestors = {"/".join(parts[:index]) for index in range(1, len(parts))}
        if ancestors.intersection(files):
            raise PatchConflictError(f"cannot create {path}; an ancestor is already a file")
        prefix = f"{path}/"
        if any(existing.startswith(prefix) for existing in files):
            raise PatchConflictError(f"cannot create {path}; it is already a directory")

    @staticmethod
    def _validate_no_file_directory_collisions(files: Mapping[str, str]) -> None:
        for path in files:
            parts = path.split("/")
            for index in range(1, len(parts)):
                if "/".join(parts[:index]) in files:
                    raise PatchValidationError(
                        f"a file and directory share the same path prefix: {path}"
                    )

    def _read_workspace_files(self) -> dict[str, str]:
        self._ensure_private_tree()
        files: dict[str, str] = {}
        for directory, directory_names, file_names in os.walk(self._root, topdown=True, followlinks=False):
            current_directory = Path(directory)
            safe_directories: list[str] = []
            for directory_name in directory_names:
                child = current_directory / directory_name
                if _is_link_or_reparse_point(child):
                    raise PatchWorkspaceError("workspace contains a symbolic link or reparse point")
                safe_directories.append(directory_name)
            directory_names[:] = safe_directories
            for file_name in file_names:
                candidate = current_directory / file_name
                if _is_link_or_reparse_point(candidate):
                    raise PatchWorkspaceError("workspace contains a symbolic link or reparse point")
                file_stat = candidate.stat()
                if not stat.S_ISREG(file_stat.st_mode):
                    raise PatchWorkspaceError("workspace contains a non-regular file")
                relative_path = candidate.relative_to(self._root).as_posix()
                safe_path = self._validate_path(relative_path)
                try:
                    raw_content = candidate.read_bytes()
                except OSError as exc:
                    raise PatchWorkspaceError(f"unable to read workspace file: {safe_path}") from exc
                files[safe_path] = _coerce_text(raw_content, f"workspace file {safe_path}")
        self._validate_file_set(files, context="workspace files")
        return files

    def _write_result_files(
        self,
        current_files: Mapping[str, str],
        resulting_files: Mapping[str, str],
    ) -> None:
        self._ensure_private_tree()
        obsolete_files = set(current_files).difference(resulting_files)
        # A proposal can legitimately replace a file with a directory (or the
        # reverse).  Remove only the obsolete files that block the result's
        # intended file/directory shape, then prune empty directories before
        # writing.  This still never touches a path outside this workspace.
        required_directories = {
            "/".join(path.split("/")[:index])
            for path in resulting_files
            for index in range(1, len(path.split("/")))
        }
        shape_blockers = {
            path
            for path in obsolete_files
            if path in required_directories
            or any(path.startswith(f"{result_path}/") for result_path in resulting_files)
        }
        for path in sorted(shape_blockers, key=lambda value: (value.count("/"), value), reverse=True):
            target = self._existing_target(path)
            try:
                target.unlink()
            except OSError as exc:
                raise PatchWorkspaceError(f"unable to delete workspace file: {path}") from exc
        if shape_blockers:
            self._prune_empty_directories()
        # Write new/changed files before deleting obsolete files.  Every write
        # uses a same-directory temporary file and os.replace, so a reader can
        # never observe a partially written individual source file.
        for path in sorted(resulting_files):
            if current_files.get(path) == resulting_files[path]:
                continue
            target = self._materialize_target(path)
            self._atomic_write_text(target, resulting_files[path])
        for path in sorted(obsolete_files.difference(shape_blockers), reverse=True):
            target = self._existing_target(path)
            try:
                target.unlink()
            except OSError as exc:
                raise PatchWorkspaceError(f"unable to delete workspace file: {path}") from exc
        self._prune_empty_directories()

    def _materialize_target(self, path: str) -> Path:
        safe_path = self._validate_path(path)
        target = self._root
        parts = safe_path.split("/")
        for part in parts[:-1]:
            target = target / part
            if target.exists():
                if _is_link_or_reparse_point(target) or not target.is_dir():
                    raise PatchWorkspaceError("unsafe workspace directory path")
            else:
                target.mkdir(mode=0o700)
        target = target / parts[-1]
        self._assert_target_within_root(target)
        if target.exists() and (target.is_dir() or _is_link_or_reparse_point(target)):
            raise PatchWorkspaceError("unsafe workspace file target")
        return target

    def _existing_target(self, path: str) -> Path:
        target = self._root.joinpath(*self._validate_path(path).split("/"))
        self._assert_target_within_root(target)
        if not target.exists() or _is_link_or_reparse_point(target) or not target.is_file():
            raise PatchWorkspaceError("workspace file changed unexpectedly")
        return target

    def _atomic_write_text(self, target: Path, content: str) -> None:
        encoded = _utf8_bytes(content, str(target))
        temp_file: Path | None = None
        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".patch-write-",
                suffix=".tmp",
                dir=target.parent,
            )
            temp_file = Path(temporary_name)
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_file, target)
        except OSError as exc:
            raise PatchWorkspaceError(f"unable to write workspace file: {target.name}") from exc
        finally:
            if temp_file is not None and temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass

    def _prune_empty_directories(self) -> None:
        for directory, _directory_names, _file_names in os.walk(self._root, topdown=False, followlinks=False):
            candidate = Path(directory)
            if candidate == self._root:
                continue
            if _is_link_or_reparse_point(candidate):
                raise PatchWorkspaceError("workspace contains a symbolic link or reparse point")
            try:
                candidate.rmdir()
            except OSError:
                # Nonempty directories are expected where another source file
                # remains; any other failure will be caught on future access.
                pass

    def _ensure_private_tree(self) -> None:
        self._ensure_open()
        if not self._container.exists() or not self._root.exists():
            raise PatchWorkspaceError("temporary workspace directory is unavailable")
        if _is_link_or_reparse_point(self._container) or _is_link_or_reparse_point(self._root):
            raise PatchWorkspaceError("temporary workspace path is unsafe")
        self._assert_target_within_root(self._root)

    def _assert_target_within_root(self, target: Path) -> None:
        try:
            target.resolve().relative_to(self._root.resolve())
        except ValueError as exc:
            raise PatchWorkspaceError("workspace path escaped its temporary root") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise PatchWorkspaceClosedError("temporary patch workspace has already been cleaned up")

    @staticmethod
    def _build_unified_diff(planned_changes: Sequence[_PlannedChange]) -> str:
        fragments: list[str] = []
        for item in planned_changes:
            before_lines = item.before.splitlines() if item.before is not None else []
            after_lines = item.after.splitlines() if item.after is not None else []
            from_file = f"a/{item.path}" if item.before is not None else "/dev/null"
            to_file = f"b/{item.path}" if item.after is not None else "/dev/null"
            fragments.extend(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=from_file,
                    tofile=to_file,
                    lineterm="\n",
                )
            )
        return "".join(fragments)


def _require_mapping(payload: object, name: str) -> None:
    if not isinstance(payload, Mapping):
        raise PatchValidationError(f"{name} must be an object")


def _reject_unknown_keys(payload: Mapping[str, Any], allowed_keys: set[str], name: str) -> None:
    unknown_keys = set(payload).difference(allowed_keys)
    if unknown_keys:
        names = ", ".join(sorted(map(str, unknown_keys)))
        raise PatchValidationError(f"{name} contains unsupported field(s): {names}")


def _validate_metadata_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PatchValidationError(f"{name} must be non-empty text no longer than {maximum} characters")
    if "\x00" in value or any(ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in value):
        raise PatchValidationError(f"{name} contains an unsafe control character")
    return value


def _normalize_sha256(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64:
        raise PatchValidationError("expected_sha256 must be a 64-character hexadecimal SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise PatchValidationError("expected_sha256 must be a 64-character hexadecimal SHA-256 digest") from exc
    return value.lower()


def _coerce_text(value: object, context: str) -> str:
    if isinstance(value, str):
        _utf8_bytes(value, context)
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchValidationError(f"{context} must be UTF-8 text, not binary content") from exc
    raise PatchValidationError(f"{context} must be text or UTF-8 bytes")


def _utf8_bytes(value: str, context: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PatchValidationError(f"{context} cannot be encoded as UTF-8") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(_utf8_bytes(value, "source content")).hexdigest()


def _total_bytes(files: Mapping[str, str]) -> int:
    return sum(len(_utf8_bytes(content, f"content for {path}")) for path, content in files.items())


def _is_windows_reserved_name(path_part: str) -> bool:
    # Windows ignores trailing dots/spaces when resolving names and reserves
    # these device names even with a normal extension (for example CON.txt).
    normalized = path_part.rstrip(". ").split(".", 1)[0].upper()
    return normalized in {"CON", "PRN", "AUX", "NUL", "CLOCK$"} or (
        len(normalized) == 4
        and normalized[:3] in {"COM", "LPT"}
        and normalized[3] in "123456789"
    )


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise PatchWorkspaceError("unable to inspect temporary workspace path") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(path_stat, "st_file_attributes", 0) & reparse_attribute)
