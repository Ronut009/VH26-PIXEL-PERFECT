"""Bounded, in-memory source context for GitHub diagnosis requests.

Snapshots intentionally persist only Git object metadata. This module retrieves
the smallest useful set of text blobs on demand, verifies each immutable blob
SHA, and returns an in-memory context bundle. It never writes source contents
to SQLite, GitHub, or the repository checkout.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import re
from typing import Protocol


class SourceContextError(ValueError):
    """Raised when source context would be unsafe, inconsistent, or unusable."""


class BlobReader(Protocol):
    async def get_blob(self, owner: str, repository: str, blob_sha: str, token: object) -> object:
        """Return a GitHub blob DTO with sha, size, and content attributes."""


@dataclass(frozen=True, slots=True)
class SourceContextPolicy:
    """Hard limits that keep diagnosis context bounded and text-only."""

    max_files: int = 12
    max_file_bytes: int = 64 * 1024
    max_total_bytes: int = 256 * 1024
    max_path_length: int = 512
    allowed_suffixes: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                ".c",
                ".cc",
                ".cpp",
                ".cs",
                ".css",
                ".go",
                ".h",
                ".hpp",
                ".html",
                ".java",
                ".js",
                ".json",
                ".jsx",
                ".kt",
                ".md",
                ".php",
                ".py",
                ".rb",
                ".rs",
                ".sh",
                ".sql",
                ".toml",
                ".ts",
                ".tsx",
                ".txt",
                ".yaml",
                ".yml",
            }
        )
    )

    def __post_init__(self) -> None:
        for name, value in (
            ("max_files", self.max_files),
            ("max_file_bytes", self.max_file_bytes),
            ("max_total_bytes", self.max_total_bytes),
            ("max_path_length", self.max_path_length),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SourceContextError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class SourceFileReference:
    path: str
    blob_sha: str
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class SourceExcerpt:
    path: str
    blob_sha: str
    text: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class SourceContext:
    excerpts: tuple[SourceExcerpt, ...]
    omitted_file_count: int
    total_bytes: int
    digest: str


_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}", re.IGNORECASE)
_GENERATED_DIRECTORY_PARTS = frozenset(
    {".next", "build", "coverage", "dist", "node_modules", "vendor"}
)
_SENSITIVE_DIRECTORY_PARTS = frozenset(
    {".aws", ".ssh", "credentials", "secrets"}
)
_SENSITIVE_FILE_NAMES = frozenset({"id_ed25519", "id_rsa", ".env"})
_GENERATED_FILE_SUFFIXES = (".map", ".min.css", ".min.js", ".lock")


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceContextError(f"{field_name} must be a non-empty string")
    return value.strip()


def _is_safe_path(path: str, policy: SourceContextPolicy) -> bool:
    # Git tree paths are POSIX-relative. Treating a backslash as a separator
    # here would make the source-selection boundary disagree with the local
    # patch-workspace boundary on Windows, so reject it rather than normalizing
    # it. NUL is likewise never a valid repository path.
    if (
        len(path) > policy.max_path_length
        or "\\" in path
        or "\x00" in path
        or path.startswith(("/", "\\"))
    ):
        return False
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    normalized_parts = tuple(part.lower() for part in parts)
    file_name = normalized_parts[-1]
    if (
        any(part in _GENERATED_DIRECTORY_PARTS for part in normalized_parts[:-1])
        or any(part in _SENSITIVE_DIRECTORY_PARTS for part in normalized_parts)
        or file_name in _SENSITIVE_FILE_NAMES
        or file_name.startswith(".env.")
        or file_name.endswith(_GENERATED_FILE_SUFFIXES)
    ):
        return False
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return f".{suffix}" in policy.allowed_suffixes


def _reference_from_row(row: Mapping[str, object], policy: SourceContextPolicy) -> SourceFileReference | None:
    if row.get("object_type") not in {None, "blob"}:
        return None
    path = row.get("path")
    blob_sha = row.get("blob_sha", row.get("sha"))
    if not isinstance(path, str) or not isinstance(blob_sha, str):
        return None
    if not _is_safe_path(path, policy):
        return None
    size = row.get("size_bytes", row.get("size"))
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        return None
    if isinstance(size, int) and size > policy.max_file_bytes:
        return None
    return SourceFileReference(path=path, blob_sha=_text(blob_sha, "blob_sha"), size_bytes=size)


def _signal_tokens(signals: Iterable[str]) -> frozenset[str]:
    tokens: set[str] = set()
    for signal in signals:
        if not isinstance(signal, str):
            continue
        for match in _TOKEN_PATTERN.finditer(signal):
            token = match.group(0).lower()
            tokens.add(token)
            tokens.update(part for part in re.split(r"[_-]+", token) if len(part) >= 2)
    return frozenset(tokens)


def select_source_files(
    rows: Sequence[Mapping[str, object]],
    *,
    signals: Iterable[str],
    policy: SourceContextPolicy,
) -> tuple[SourceFileReference, ...]:
    """Return deterministic, safe candidates ranked by incident signal/path match."""

    tokens = _signal_tokens(signals)
    references: list[SourceFileReference] = []
    seen_paths: set[str] = set()
    for row in rows:
        reference = _reference_from_row(row, policy)
        if reference is None or reference.path in seen_paths:
            continue
        seen_paths.add(reference.path)
        references.append(reference)

    def rank(reference: SourceFileReference) -> tuple[int, int, str]:
        path_tokens = _signal_tokens((reference.path,))
        signal_matches = len(tokens.intersection(path_tokens))
        size = reference.size_bytes if reference.size_bytes is not None else policy.max_file_bytes
        return (-signal_matches, size, reference.path)

    return tuple(sorted(references, key=rank)[: policy.max_files])


async def build_source_context(
    client: BlobReader,
    *,
    owner: str,
    repository: str,
    token: object,
    snapshot_rows: Sequence[Mapping[str, object]],
    signals: Iterable[str],
    policy: SourceContextPolicy,
) -> SourceContext:
    """Fetch selected immutable blobs in memory and return a bounded text bundle."""

    owner = _text(owner, "owner")
    repository = _text(repository, "repository")
    candidates = select_source_files(snapshot_rows, signals=signals, policy=policy)
    excerpts: list[SourceExcerpt] = []
    total_bytes = 0

    for reference in candidates:
        blob = await client.get_blob(owner, repository, reference.blob_sha, token)
        response_sha = getattr(blob, "sha", None)
        content = getattr(blob, "content", None)
        response_size = getattr(blob, "size", None)
        if response_sha != reference.blob_sha:
            raise SourceContextError("GitHub blob identity did not match the snapshot")
        if isinstance(response_size, bool) or not isinstance(response_size, int) or response_size < 0:
            raise SourceContextError("GitHub blob size was invalid")
        if not isinstance(content, bytes) or len(content) != response_size:
            raise SourceContextError("GitHub blob content did not match its declared size")
        if reference.size_bytes is not None and response_size != reference.size_bytes:
            raise SourceContextError("GitHub blob size did not match the immutable snapshot")
        if response_size > policy.max_file_bytes:
            continue
        if total_bytes + response_size > policy.max_total_bytes:
            continue
        if b"\x00" in content:
            continue

        # A replacement decode can manufacture source text that was never in
        # the pinned blob. Skip malformed UTF-8 instead: Phase 2 is a
        # text-only, evidence-preserving context path, not a binary decoder.
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue

        excerpts.append(
            SourceExcerpt(
                path=reference.path,
                blob_sha=reference.blob_sha,
                text=text,
                byte_count=response_size,
            )
        )
        total_bytes += response_size

    digest_material = "\n".join(
        f"{excerpt.path}\x00{excerpt.blob_sha}\x00{excerpt.byte_count}"
        for excerpt in excerpts
    )
    digest = hashlib.sha256(digest_material.encode("utf-8")).hexdigest()
    return SourceContext(
        excerpts=tuple(excerpts),
        omitted_file_count=max(0, len(candidates) - len(excerpts)),
        total_bytes=total_bytes,
        digest=digest,
    )
