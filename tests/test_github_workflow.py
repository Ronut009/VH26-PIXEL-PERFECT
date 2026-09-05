"""Tests for the pure Phase 2--4 conversion and patch-scope helpers."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.github_integration.diagnosis import (
    DiagnosisIncidentContext,
    SourceSnapshotReference,
)
from src.github_integration.source_context import SourceContext, SourceExcerpt
from src.github_integration.workflow import (
    bind_patch_to_snapshot,
    build_diagnosis_request,
    language_for_path,
    patch_review_payload,
)
from src.github_integration.workspace import (
    ChangeAction,
    LocalPatchWorkspace,
    PatchValidationError,
    ProposedFileChange,
    ProposedPatch,
)


INCIDENT_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40
BLOB_SHA = "c" * 40
SOURCE = "def checkout(payment):\n    return charge(payment)\n"


def _incident() -> DiagnosisIncidentContext:
    return DiagnosisIncidentContext(
        incident_id=INCIDENT_ID,
        service="checkout-api",
        alertname="CheckoutErrorRateHigh",
        severity="critical",
        status="firing",
        scope_key="production:payments-east",
        alert_count=100,
        message="checkout 5xx rate rose",
        labels={"environment": "production", "cluster": "payments-east"},
    )


def _snapshot() -> SourceSnapshotReference:
    return SourceSnapshotReference(
        snapshot_id=SNAPSHOT_ID,
        repository_id=8123,
        repository_full_name="acme/checkout-api",
        commit_sha=COMMIT_SHA,
        tree_sha=TREE_SHA,
    )


def _source_context() -> SourceContext:
    return SourceContext(
        excerpts=(
            SourceExcerpt(
                path="src/handlers/checkout.py",
                blob_sha=BLOB_SHA,
                text=SOURCE,
                byte_count=len(SOURCE.encode("utf-8")),
            ),
        ),
        omitted_file_count=0,
        total_bytes=len(SOURCE.encode("utf-8")),
        digest="d" * 64,
    )


def test_build_diagnosis_request_binds_source_to_the_exact_snapshot() -> None:
    request = build_diagnosis_request(
        incident=_incident(), snapshot=_snapshot(), source_context=_source_context()
    )

    assert request.snapshot.commit_sha == COMMIT_SHA
    assert request.excerpts[0].file_path == "src/handlers/checkout.py"
    assert request.excerpts[0].blob_sha == BLOB_SHA
    assert request.excerpts[0].start_line == 1
    assert request.excerpts[0].end_line == 3
    assert request.excerpts[0].language == "python"
    assert language_for_path("web/dashboard.tsx") == "typescript"
    assert language_for_path("LICENSE") is None


def test_build_diagnosis_request_refuses_oversized_or_invalid_source_contracts() -> None:
    oversized = replace(
        _source_context().excerpts[0], text="x" * (8 * 1024 + 1), byte_count=8 * 1024 + 1
    )
    context = SourceContext(
        excerpts=(oversized,),
        omitted_file_count=0,
        total_bytes=oversized.byte_count,
        digest="d" * 64,
    )

    with pytest.raises(ValidationError, match="UTF-8 bytes"):
        build_diagnosis_request(incident=_incident(), snapshot=_snapshot(), source_context=context)


def test_patch_scope_binds_expected_hashes_and_rejects_unsupplied_or_create_paths(tmp_path) -> None:
    path = "src/handlers/checkout.py"
    base_files = {path: SOURCE}
    proposal = ProposedPatch(
        patch_id="checkout-fix",
        summary="Add an error boundary.",
        changes=(
            ProposedFileChange(
                path=path,
                action=ChangeAction.UPDATE,
                content="def checkout(payment):\n    return safe_charge(payment)\n",
            ),
        ),
    )

    bound = bind_patch_to_snapshot(proposal, base_files=base_files, allowed_paths=[path])
    assert bound.changes[0].expected_sha256 == hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()
    with LocalPatchWorkspace(base_files, temp_parent=tmp_path) as workspace:
        review = workspace.apply(bound)
    payload = patch_review_payload(review)
    assert payload["changed_files"][0]["path"] == path
    assert payload["metadata"]["human_review_required"] is True

    ungrounded = replace(
        proposal,
        changes=(
            ProposedFileChange(
                path="src/other.py",
                action=ChangeAction.UPDATE,
                content="print('unsafe')\n",
            ),
        ),
    )
    with pytest.raises(PatchValidationError, match="supplied"):
        bind_patch_to_snapshot(ungrounded, base_files=base_files, allowed_paths=[path])

    creates_file = replace(
        proposal,
        changes=(
            ProposedFileChange(
                path="tests/test_checkout.py",
                action=ChangeAction.CREATE,
                content="pass\n",
            ),
        ),
    )
    with pytest.raises(PatchValidationError, match="updates or deletions"):
        bind_patch_to_snapshot(creates_file, base_files=base_files, allowed_paths=[path])
