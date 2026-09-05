"""Tests for bounded, in-memory GitHub source context construction."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.github_integration.source_context import (
    SourceContextError,
    SourceContextPolicy,
    build_source_context,
    select_source_files,
)


@dataclass
class _Blob:
    sha: str
    content: bytes
    size: int


class _BlobClient:
    def __init__(self, blobs: dict[str, _Blob]) -> None:
        self.blobs = blobs
        self.calls: list[str] = []

    async def get_blob(self, owner: str, repository: str, blob_sha: str, token: object) -> _Blob:
        assert (owner, repository, token) == ("acme", "checkout", "token")
        self.calls.append(blob_sha)
        return self.blobs[blob_sha]


def test_select_source_files_is_safe_relevant_and_deterministic() -> None:
    policy = SourceContextPolicy(max_files=2, max_file_bytes=100, max_total_bytes=150)
    rows = [
        {"path": "src/checkout_handler.py", "blob_sha": "checkout", "size_bytes": 30},
        {"path": "docs/architecture.md", "blob_sha": "docs", "size_bytes": 20},
        {"path": "../../secrets.py", "blob_sha": "unsafe", "size_bytes": 1},
        {"path": "assets/bundle.min.js", "blob_sha": "large", "size_bytes": 101},
        {"path": "image.png", "blob_sha": "image", "size_bytes": 2},
    ]

    selected = select_source_files(rows, signals=["checkout latency"], policy=policy)

    assert [item.path for item in selected] == ["src/checkout_handler.py", "docs/architecture.md"]


def test_select_source_files_rejects_windows_style_and_nul_containing_paths() -> None:
    selected = select_source_files(
        [
            {"path": "src\\checkout.py", "blob_sha": "windows", "size_bytes": 10},
            {"path": "src/checkout.py\x00", "blob_sha": "nul", "size_bytes": 10},
            {"path": "src/checkout.py", "blob_sha": "safe", "size_bytes": 10},
        ],
        signals=["checkout"],
        policy=SourceContextPolicy(),
    )

    assert [item.path for item in selected] == ["src/checkout.py"]


def test_select_source_files_excludes_detectable_secrets_and_generated_assets() -> None:
    selected = select_source_files(
        [
            {"path": "secrets/production.py", "blob_sha": "secret", "size_bytes": 10},
            {"path": ".env.production", "blob_sha": "env", "size_bytes": 10},
            {"path": "dist/app.min.js", "blob_sha": "generated", "size_bytes": 10},
            {"path": "src/checkout.py", "blob_sha": "safe", "size_bytes": 10},
        ],
        signals=["checkout"],
        policy=SourceContextPolicy(),
    )

    assert [item.path for item in selected] == ["src/checkout.py"]


@pytest.mark.asyncio
async def test_build_source_context_keeps_text_in_memory_and_enforces_budget() -> None:
    policy = SourceContextPolicy(max_files=3, max_file_bytes=50, max_total_bytes=40)
    client = _BlobClient(
        {
            "a": _Blob("a", b"def checkout():\n    return 'ok'\n", 32),
            "b": _Blob("b", b"x" * 20, 20),
            "binary": _Blob("binary", b"\x00\x01", 2),
        }
    )
    rows = [
        {"path": "src/checkout.py", "blob_sha": "a", "size_bytes": 32},
        {"path": "src/worker.py", "blob_sha": "b", "size_bytes": 20},
        {"path": "src/binary.py", "blob_sha": "binary", "size_bytes": 2},
    ]

    context = await build_source_context(
        client,
        owner="acme",
        repository="checkout",
        token="token",
        snapshot_rows=rows,
        signals=["checkout"],
        policy=policy,
    )

    assert [excerpt.path for excerpt in context.excerpts] == ["src/checkout.py"]
    assert context.total_bytes == 32
    assert context.omitted_file_count == 2
    assert len(context.digest) == 64
    assert client.calls == ["a", "binary", "b"]


@pytest.mark.asyncio
async def test_build_source_context_rejects_a_blob_that_does_not_match_snapshot() -> None:
    client = _BlobClient({"expected": _Blob("different", b"print('x')", 10)})

    with pytest.raises(SourceContextError, match="identity"):
        await build_source_context(
            client,
            owner="acme",
            repository="checkout",
            token="token",
            snapshot_rows=[{"path": "src/app.py", "blob_sha": "expected", "size_bytes": 10}],
            signals=[],
            policy=SourceContextPolicy(),
        )


@pytest.mark.asyncio
async def test_build_source_context_skips_malformed_utf8_instead_of_replacing_bytes() -> None:
    client = _BlobClient({"bad": _Blob("bad", b"\xff\xfe", 2)})

    context = await build_source_context(
        client,
        owner="acme",
        repository="checkout",
        token="token",
        snapshot_rows=[{"path": "src/bad.py", "blob_sha": "bad", "size_bytes": 2}],
        signals=["checkout"],
        policy=SourceContextPolicy(),
    )

    assert context.excerpts == ()
    assert context.omitted_file_count == 1
