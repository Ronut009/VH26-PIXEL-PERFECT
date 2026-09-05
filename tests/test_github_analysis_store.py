"""Focused persistence tests for sanitized GitHub diagnosis results."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import aiosqlite
import pytest
import pytest_asyncio

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
from src.github_integration.diagnosis import (
    DiagnosisEvidence,
    DiagnosisRequest,
    DiagnosisResult,
    ProposedFix,
    RootCauseHypothesis,
    SourceExcerpt,
    safe_fallback,
)


SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"
INCIDENT_ID = UUID("11111111-1111-1111-1111-111111111111")
EVENT_ID = UUID("22222222-2222-2222-2222-222222222222")
SNAPSHOT_ID = UUID("33333333-3333-3333-3333-333333333333")
COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40
BLOB_SHA = "c" * 40


@pytest_asyncio.fixture
async def db_conn() -> aiosqlite.Connection:
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    await _seed_database(connection)
    yield connection
    await connection.close()


async def _seed_database(connection: aiosqlite.Connection) -> None:
    """Create the smallest valid engine + Phase 1 binding without source data."""

    await connection.execute(
        """
        INSERT INTO incidents (
            incident_id, scope_key, stable_fingerprint, title, summary, severity,
            status, alert_count, first_alert_at, last_alert_at, root_cause_hint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(INCIDENT_ID),
            "production:payments-east",
            "stable-checkout",
            "Checkout error rate is high",
            "The dashboard coalesced repeated checkout failures.",
            "critical",
            "OPEN",
            100,
            "2026-09-04T10:00:00Z",
            "2026-09-04T10:05:00Z",
            "root_cause=incident-leader",
        ),
    )
    await connection.execute(
        """
        INSERT INTO raw_events (
            event_id, seq, fingerprint, stable_fingerprint, scope_key, source,
            service, alertname, severity_raw, status, labels_json, message,
            fired_at, raw_payload, prev_hash, row_hash, incident_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(EVENT_ID),
            1,
            "fingerprint",
            "stable-checkout",
            "production:payments-east",
            "grafana",
            "checkout-api",
            "CheckoutErrorRateHigh",
            "critical",
            "firing",
            '{"environment":"production","cluster":"payments-east"}',
            "checkout 5xx rate rose after deployment",
            "2026-09-04T10:00:00Z",
            '{"raw_payload_marker":"RAW_EVENT_PAYLOAD_MUST_NOT_BE_LOADED"}',
            "0" * 64,
            "1" * 64,
            str(INCIDENT_ID),
        ),
    )
    await connection.execute(
        """
        INSERT INTO github_installations (
            installation_id, account_login, account_type, repository_selection,
            status, permissions_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (9988, "acme", "Organization", "selected", "active", '{"metadata":"read","contents":"read"}'),
    )
    await connection.execute(
        """
        INSERT INTO github_installation_state_versions (
            installation_id, provider_updated_at_us, provider_event_priority, revision
        ) VALUES (?, ?, ?, ?)
        """,
        (9988, 1, 10, 7),
    )
    await connection.execute(
        """
        INSERT INTO github_repositories (
            repository_id, installation_id, owner, name, full_name, default_branch,
            is_private, is_archived, is_selected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (8123, 9988, "acme", "checkout-api", "acme/checkout-api", "main", 1, 0, 1),
    )
    await connection.execute(
        """
        INSERT INTO github_service_mappings (service, repository_id)
        VALUES (?, ?)
        """,
        ("checkout-api", 8123),
    )
    await connection.execute(
        """
        INSERT INTO github_snapshots (
            snapshot_id, repository_id, ref, commit_sha, tree_sha, file_count, tree_truncated
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (str(SNAPSHOT_ID), 8123, "main", COMMIT_SHA, TREE_SHA, 1, 0),
    )
    await connection.commit()


async def _request_from_database(connection: aiosqlite.Connection) -> DiagnosisRequest:
    incident, binding = await load_incident_analysis_binding(connection, incident_id=INCIDENT_ID)
    return DiagnosisRequest(
        incident=incident,
        snapshot=binding.snapshot,
        excerpts=[
            SourceExcerpt(
                file_path="src/handlers/checkout.py",
                blob_sha=BLOB_SHA,
                start_line=40,
                end_line=48,
                content=(
                    "SOURCE_EXCERPT_MUST_NEVER_REACH_SQL\n"
                    "password = super-secret-source-only-value\n"
                ),
                language="python",
            )
        ],
    )


def _diagnosed_result() -> DiagnosisResult:
    return DiagnosisResult(
        status="diagnosed",
        provider="local-review-provider",
        root_cause_hypothesis=RootCauseHypothesis(
            summary="A github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 must be redacted.",
            reasoning=(
                "The alert evidence points to the checkout boundary.\n"
                "```python\n"
                "def source_code_that_must_not_persist():\n"
                "    return 'raw source'\n"
                "```"
            ),
        ),
        evidence=[
            DiagnosisEvidence(
                kind="incident",
                explanation="The coalesced incident contains 100 checkout 5xx alerts.",
            ),
            DiagnosisEvidence(
                kind="source_excerpt",
                explanation="The supplied call path lacks a known failure boundary.",
                file_path="src/handlers/checkout.py",
                blob_sha=BLOB_SHA,
                start_line=41,
                end_line=42,
            ),
        ],
        proposed_fix=ProposedFix(
            summary="Handle the expected payment error at the checkout boundary.",
            steps=[
                "Catch the expected payment failure and return the intended response.",
                "Add a regression test before merging the human-reviewed patch.",
            ],
            affected_paths=["src/handlers/checkout.py"],
        ),
        confidence=0.81,
    )


@pytest.mark.asyncio
async def test_loads_normalized_incident_and_active_service_snapshot(db_conn) -> None:
    incident = await load_incident_context(db_conn, incident_id=INCIDENT_ID)
    binding = await load_active_service_snapshot(db_conn, service="checkout-api")
    resolved_incident, resolved_binding = await load_incident_analysis_binding(
        db_conn, incident_id=INCIDENT_ID
    )

    assert incident.incident_id == INCIDENT_ID
    assert incident.service == "checkout-api"
    assert incident.labels == {"environment": "production", "cluster": "payments-east"}
    assert "RAW_EVENT_PAYLOAD_MUST_NOT_BE_LOADED" not in incident.model_dump_json()
    assert binding.service == "checkout-api"
    assert binding.repository_id == 8123
    assert binding.installation_id == 9988
    assert binding.owner == "acme"
    assert binding.repository == "checkout-api"
    assert binding.full_name == "acme/checkout-api"
    assert binding.default_branch == "main"
    assert binding.state_revision == 7
    assert binding.snapshot.snapshot_id == SNAPSHOT_ID
    assert binding.snapshot.commit_sha == COMMIT_SHA
    assert (resolved_incident, resolved_binding) == (incident, binding)


@pytest.mark.asyncio
async def test_persists_only_sanitized_whitelisted_result_projection(db_conn) -> None:
    request = await _request_from_database(db_conn)

    stored = await persist_diagnosis_result(
        db_conn,
        request=request,
        result=_diagnosed_result(),
    )
    await db_conn.commit()
    loaded = await get_diagnosis_result(db_conn, analysis_id=stored["analysis_id"])
    listed = await list_incident_diagnosis_results(db_conn, incident_id=INCIDENT_ID)

    assert loaded == stored
    assert [record["analysis_id"] for record in listed] == [stored["analysis_id"]]
    diagnosis = stored["diagnosis"]
    assert diagnosis["status"] == "diagnosed"
    assert diagnosis["provider"] == "local-review-provider"
    assert diagnosis["confidence"] == 0.81
    assert diagnosis["root_cause_hypothesis"] is not None
    assert "github_pat_" not in diagnosis["root_cause_hypothesis"]["summary"]
    assert "[GitHub token omitted]" in diagnosis["root_cause_hypothesis"]["summary"]
    assert "source_code_that_must_not_persist" not in diagnosis["root_cause_hypothesis"]["reasoning"]
    assert "[source code omitted]" in diagnosis["root_cause_hypothesis"]["reasoning"]
    assert diagnosis["proposed_fix"] is not None
    assert diagnosis["proposed_fix"]["requires_human_review"] is True
    assert diagnosis["proposed_fix"]["automatically_applied"] is False
    assert all("content" not in item for item in diagnosis["evidence"])
    assert stored["source_context"]["excerpt_count"] == 1
    assert stored["source_context"]["byte_count"] == request.source_bytes
    assert len(stored["source_context"]["digest"]) == 64

    async with db_conn.execute("SELECT * FROM github_incident_analyses") as cursor:
        raw_row = await cursor.fetchone()
    serialized_database_row = "\n".join(str(value) for value in dict(raw_row).values())
    assert "SOURCE_EXCERPT_MUST_NEVER_REACH_SQL" not in serialized_database_row
    assert "super-secret-source-only-value" not in serialized_database_row
    assert "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" not in serialized_database_row

    async with db_conn.execute("PRAGMA table_info(github_incident_analyses)") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    assert {"source_content", "raw_source", "token", "patch_json"}.isdisjoint(columns)


@pytest.mark.asyncio
async def test_persists_safe_fallback_and_does_not_require_excerpts(db_conn) -> None:
    incident, binding = await load_incident_analysis_binding(db_conn, incident_id=INCIDENT_ID)
    request = DiagnosisRequest(incident=incident, snapshot=binding.snapshot, excerpts=[])

    stored = await persist_diagnosis_result(
        db_conn,
        request=request,
        result=safe_fallback("no_source_excerpts"),
        source_context_digest="d" * 64,
    )

    diagnosis = stored["diagnosis"]
    assert diagnosis["status"] == "fallback"
    assert diagnosis["fallback"] is not None
    assert diagnosis["fallback"]["reason"] == "no_source_excerpts"
    assert diagnosis["root_cause_hypothesis"] is None
    assert diagnosis["proposed_fix"] is None
    assert stored["source_context"] == {
        "digest": "d" * 64,
        "excerpt_count": 0,
        "byte_count": 0,
    }


@pytest.mark.asyncio
async def test_refuses_inactive_mapping_or_unmapped_snapshot_before_persisting(db_conn) -> None:
    request = await _request_from_database(db_conn)
    await db_conn.execute("UPDATE github_repositories SET is_selected = 0 WHERE repository_id = 8123")

    with pytest.raises(GitHubAnalysisBindingError, match="active service mapping"):
        await persist_diagnosis_result(db_conn, request=request, result=_diagnosed_result())

    async with db_conn.execute("SELECT COUNT(*) AS count FROM github_incident_analyses") as cursor:
        assert (await cursor.fetchone())["count"] == 0


@pytest.mark.asyncio
async def test_refuses_an_incident_id_paired_with_a_different_service(db_conn) -> None:
    request = await _request_from_database(db_conn)
    mismatched_request = request.model_copy(
        update={"incident": request.incident.model_copy(update={"service": "unmapped-service"})}
    )

    with pytest.raises(GitHubAnalysisBindingError, match="does not match the stored incident"):
        await persist_diagnosis_result(
            db_conn, request=mismatched_request, result=_diagnosed_result()
        )

    async with db_conn.execute("SELECT COUNT(*) AS count FROM github_incident_analyses") as cursor:
        assert (await cursor.fetchone())["count"] == 0


@pytest.mark.asyncio
async def test_rejects_invalid_context_digest_without_writing(db_conn) -> None:
    request = await _request_from_database(db_conn)

    with pytest.raises(GitHubAnalysisStoreError, match="source_context_digest"):
        await persist_diagnosis_result(
            db_conn,
            request=request,
            result=_diagnosed_result(),
            source_context_digest="not-a-sha256",
        )

    async with db_conn.execute("SELECT COUNT(*) AS count FROM github_incident_analyses") as cursor:
        assert (await cursor.fetchone())["count"] == 0


async def _append_lifecycle_events(connection: aiosqlite.Connection) -> None:
    """The rows PulseGraph writes about itself as an incident winds down."""

    for seq, status, message in (
        (2, "firing", f"Adaptive silence deadline reached for incident {INCIDENT_ID}"),
        (3, "resolved", "No alerts for 924s, past this incident's 900s silence threshold."),
    ):
        await connection.execute(
            """
            INSERT INTO raw_events (
                event_id, seq, fingerprint, stable_fingerprint, scope_key, source,
                service, alertname, severity_raw, status, labels_json, message,
                fired_at, raw_payload, prev_hash, row_hash, incident_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"00000000-0000-4000-8000-00000000000{seq}",
                seq,
                "fingerprint",
                "stable-checkout",
                "production:payments-east",
                # PulseGraph's own lifecycle machinery, never an ingest source.
                "generic",
                "checkout-api",
                "CheckoutErrorRateHigh",
                "critical",
                status,
                "{}",
                message,
                "2026-09-04T10:20:00Z",
                "{}",
                "0" * 64,
                str(seq) * 64,
                str(INCIDENT_ID),
            ),
        )
    await connection.commit()


@pytest.mark.asyncio
async def test_context_ignores_pulsegraph_own_lifecycle_events(db_conn) -> None:
    """A resolved incident must still describe the outage, not its own closure.

    The newest rows on a resolved incident are the quiet-deadline trigger and
    the sweeper's resolution note. Fed those as the incident message, the model
    diagnoses PulseGraph's bookkeeping - correctly concluding nothing is wrong
    and proposing no fix, which fails the grounded contract and reaches the
    operator as an unexplained fallback.
    """

    await _append_lifecycle_events(db_conn)

    context = await load_incident_context(db_conn, incident_id=INCIDENT_ID)

    assert context.message == "checkout 5xx rate rose after deployment"
    assert context.status == "firing"


@pytest.mark.asyncio
async def test_context_falls_back_when_only_lifecycle_events_exist(db_conn) -> None:
    """Never prefer no context at all over an internally generated one."""

    await db_conn.execute("DELETE FROM raw_events WHERE incident_id = ?", (str(INCIDENT_ID),))
    await _append_lifecycle_events(db_conn)

    context = await load_incident_context(db_conn, incident_id=INCIDENT_ID)

    assert context.message.startswith("No alerts for 924s")
