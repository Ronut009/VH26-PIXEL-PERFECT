"""Acceptance criteria for PulseGraph's engine and transaction-bound adapter."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio

from src.config import settings
from src.contracts import NormalizedEvent
from src.engine.adaptive_ewma import (
    DEFAULT_GAP_HISTORY_MAX,
    DEFAULT_INITIAL_WINDOW_MS,
    calculate_quiet_deadline,
    retain_recent_gaps,
)
from src.engine.critical_bypass import (
    build_bypass_artifacts,
    classify_protected_critical,
)
from src.engine.db_adapter import persist_decision
from src.engine.dedupe import generate_fingerprint, is_exact_duplicate
from src.engine.incident_machine import transition_state
from src.engine.process_event import process_event
from src.engine.timer_wheel import TimerWheel

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


@pytest_asyncio.fixture
async def engine_db() -> aiosqlite.Connection:
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield connection
    await connection.close()


def _event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "scope_key": "production/payments",
        "service": "payment-api",
        "alertname": "HighCPUUsage",
        "severity": "warning",
        "status": "firing",
        "labels": {
            "environment": "production",
            "region": "ap-south-1",
            "pod": "payment-api-7cb4f6d9dc-a1b2c",
            "pod_uid": "11111111-1111-1111-1111-111111111111",
            "timestamp": "2026-09-04T12:00:00Z",
        },
        "fired_at": "2026-09-04T12:00:00Z",
    }
    event.update(overrides)
    return event


def test_dedupe_ignores_pod_identity_and_timestamps() -> None:
    first = _event()
    retry_on_a_different_pod = _event(
        labels={
            "environment": "production",
            "region": "ap-south-1",
            "pod": "payment-api-7cb4f6d9dc-z9y8x",
            "pod_uid": "22222222-2222-2222-2222-222222222222",
            "timestamp": "2026-09-04T12:00:05Z",
        },
        fired_at="2026-09-04T12:00:05Z",
    )

    assert is_exact_duplicate(first, retry_on_a_different_pod, "production/payments")


def test_dedupe_never_crosses_scope_or_service_boundaries() -> None:
    first = _event()
    other_scope = _event(scope_key="staging/payments")
    other_service = _event(service="checkout-api")

    assert not is_exact_duplicate(first, other_scope, "production/payments")
    assert not is_exact_duplicate(first, other_service, "production/payments")


def test_dedupe_fingerprint_is_stable_for_equivalent_normalized_dicts() -> None:
    first = _event()
    equivalent = _event(
        labels={
            "region": "ap-south-1",
            "environment": "production",
            "pod": "different-pod",
            "pod_uid": "different-uid",
            "timestamp": "later",
        }
    )

    assert generate_fingerprint(first) == generate_fingerprint(equivalent)


def test_ewma_uses_short_deadline_for_rapid_alerts() -> None:
    current_time = 10_000
    result = calculate_quiet_deadline([100.0, 120.0, 80.0], 90.0, current_time)

    assert result["mean_gap"] > 0
    assert result["variance"] >= 0
    assert result["quiet_at_ms"] > current_time
    assert result["quiet_at_ms"] - current_time < 500


def test_ewma_uses_longer_deadline_for_sporadic_alerts() -> None:
    current_time = 10_000
    rapid = calculate_quiet_deadline([100.0, 120.0, 80.0], 90.0, current_time)
    sporadic = calculate_quiet_deadline([5_000.0, 6_500.0, 7_000.0], 6_000.0, current_time)

    assert sporadic["mean_gap"] > rapid["mean_gap"]
    assert sporadic["quiet_at_ms"] > rapid["quiet_at_ms"]


def test_ewma_predicts_exact_deadline_for_a_stable_cadence() -> None:
    result = calculate_quiet_deadline([100.0, 100.0], 100.0, 10_000)

    assert result == {
        "quiet_at_ms": 10_100,
        "mean_gap": 100.0,
        "variance": 0.0,
    }


def test_ewma_empty_gap_returns_default_window() -> None:
    current_time = 10_000
    result = calculate_quiet_deadline([], 0.0, current_time)

    assert result == {
        "quiet_at_ms": current_time + DEFAULT_INITIAL_WINDOW_MS,
        "mean_gap": 0.0,
        "variance": 0.0,
    }


def test_ewma_handles_zero_gap_and_rejects_negative_gap() -> None:
    assert calculate_quiet_deadline([0.0, 0.0], 0.0, 100) == {
        "quiet_at_ms": 101,
        "mean_gap": 0.0,
        "variance": 0.0,
    }

    with pytest.raises(ValueError, match="non-negative"):
        calculate_quiet_deadline([10.0], -1.0, 100)


def test_machine_accepts_the_strict_forward_lifecycle() -> None:
    acknowledged = transition_state("OPEN", "ACKNOWLEDGE")
    quiescent = transition_state(acknowledged, "QUIET_TIMEOUT")
    resolved = transition_state(quiescent, "RESOLVE")

    assert acknowledged == "ACKNOWLEDGED"
    assert quiescent == "QUIESCENT"
    assert resolved == "RESOLVED"


def test_machine_only_reopens_resolved_incidents() -> None:
    assert transition_state("RESOLVED", "REOPEN") == "OPEN"
    assert transition_state("OPEN", "REOPEN") is None


def test_machine_resolves_from_every_active_state() -> None:
    """A fix can land while an incident is still firing.

    Incidents are created directly in ACKNOWLEDGED, so when RESOLVE was
    reachable only from QUIESCENT, a `resolved` webhook - or an operator
    resolving in PagerDuty - was dropped and the incident never closed.
    """

    assert transition_state("OPEN", "RESOLVE") == "RESOLVED"
    assert transition_state("ACKNOWLEDGED", "RESOLVE") == "RESOLVED"
    assert transition_state("QUIESCENT", "RESOLVE") == "RESOLVED"


@pytest.mark.parametrize(
    ("current_state", "trigger"),
    [
        ("ACKNOWLEDGED", "REOPEN"),
        ("QUIESCENT", "ACKNOWLEDGE"),
        ("RESOLVED", "ACKNOWLEDGE"),
        ("UNKNOWN", "ACKNOWLEDGE"),
        ("OPEN", "UNKNOWN"),
    ],
)
def test_machine_rejects_invalid_transitions(current_state: str, trigger: str) -> None:
    assert transition_state(current_state, trigger) is None


def _normalized_event(
    *,
    fired_at: datetime,
    severity_raw: str = "warning",
    status: str = "firing",
    pod: str = "payment-api-a",
    service: str = "payment-api",
    alertname: str = "HighCPUUsage",
    message: str = "CPU above threshold",
    extra_labels: dict[str, str] | None = None,
) -> NormalizedEvent:
    labels = {
        "environment": "production",
        "cluster": "payments",
        "pod": pod,
        "pod_uid": f"uid-{pod}",
    }
    if extra_labels:
        labels.update(extra_labels)
    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint="ingest-fingerprint-is-not-the-engine-key",
        source="prometheus",
        service=service,
        alertname=alertname,
        severity_raw=severity_raw,
        status=status,  # type: ignore[arg-type]
        labels=labels,
        message=message,
        fired_at=fired_at,
        raw_payload={"labels": labels},
    )


@pytest.mark.asyncio
async def test_process_event_coalesces_a_duplicate_without_writing(engine_db) -> None:
    first_at = datetime(2026, 9, 4, tzinfo=timezone.utc)
    first = _normalized_event(fired_at=first_at, pod="payment-api-a")
    first_decision = await process_event(engine_db, first)
    await persist_decision(engine_db, first_decision)
    await engine_db.commit()

    retry = _normalized_event(
        fired_at=first_at + timedelta(milliseconds=100),
        pod="payment-api-b",
    )
    outcome = await process_event(engine_db, retry)

    assert outcome.incident_id == first_decision.incident_id
    assert outcome.is_duplicate is True
    assert outcome.is_critical_bypass is False
    assert outcome.state == "ACKNOWLEDGED"
    assert outcome.quiet_at_ms is not None
    assert any(change.kind == "DUPLICATE_COALESCED" for change in outcome.card_changes)


@pytest.mark.asyncio
async def test_process_event_creates_acknowledged_incident(engine_db) -> None:
    event = _normalized_event(fired_at=datetime(2026, 9, 4, tzinfo=timezone.utc))

    outcome = await process_event(engine_db, event)

    assert outcome.is_duplicate is False
    assert outcome.state == "ACKNOWLEDGED"
    assert outcome.quiet_at_ms is not None
    assert any(change.kind == "STATE_OPEN_TO_ACKNOWLEDGED" for change in outcome.card_changes)


@pytest.mark.asyncio
async def test_process_event_bypasses_filtering_for_protected_critical(engine_db) -> None:
    event = _normalized_event(
        fired_at=datetime(2026, 9, 4, tzinfo=timezone.utc), severity_raw="critical"
    )

    outcome = await process_event(engine_db, event)

    assert outcome.is_critical_bypass is True
    assert outcome.is_duplicate is False
    assert outcome.state == "ACKNOWLEDGED"
    assert outcome.quiet_at_ms is None
    assert outcome.bypass_reason == "SEVERITY_CRITICAL"
    assert {intent.channel for intent in outcome.delivery_intents} == {"pagerduty", "slack"}


@pytest.mark.asyncio
async def test_process_event_reopens_a_resolved_incident(engine_db) -> None:
    first_at = datetime(2026, 9, 4, tzinfo=timezone.utc)
    first = _normalized_event(fired_at=first_at)
    initial = await process_event(engine_db, first)
    await persist_decision(engine_db, initial)
    await engine_db.execute(
        "UPDATE incidents SET status = 'RESOLVED' WHERE incident_id = ?",
        (str(initial.incident_id),),
    )
    await engine_db.commit()

    firing_again = _normalized_event(fired_at=first_at + timedelta(seconds=1))
    outcome = await process_event(engine_db, firing_again)

    assert outcome.incident_id == initial.incident_id
    assert outcome.state == "ACKNOWLEDGED"
    assert any(change.kind == "STATE_RESOLVED_TO_ACKNOWLEDGED" for change in outcome.card_changes)


def test_timer_wheel_emits_deadlines_in_time_order() -> None:
    wheel = TimerWheel()
    later_id = uuid4()
    earlier_id = uuid4()

    wheel.schedule(later_id, 300)
    wheel.schedule(earlier_id, 100)

    assert wheel.pop_due(99) == ()
    due = wheel.pop_due(100)
    assert len(due) == 1
    assert due[0].incident_id == earlier_id
    assert due[0].trigger == "QUIET_DEADLINE"
    assert transition_state("ACKNOWLEDGED", due[0].trigger) == "QUIESCENT"
    assert wheel.pop_due(299) == ()
    assert wheel.pop_due(300)[0].incident_id == later_id


def test_timer_wheel_discards_stale_deadlines_after_reschedule() -> None:
    wheel = TimerWheel()
    incident_id = uuid4()
    wheel.schedule(incident_id, 100)
    wheel.schedule(incident_id, 200)

    assert wheel.pop_due(100) == ()
    due = wheel.pop_due(200)
    assert len(due) == 1
    assert due[0].incident_id == incident_id
    assert due[0].quiet_at_ms == 200


def test_timer_wheel_is_safe_for_concurrent_scheduling() -> None:
    wheel = TimerWheel()
    incident_ids = [uuid4() for _ in range(32)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda item: wheel.schedule(item, 1_000), incident_ids))

    due = wheel.pop_due(1_000)
    assert {trigger.incident_id for trigger in due} == set(incident_ids)


def test_gap_history_is_bounded_and_keeps_the_most_recent_gaps() -> None:
    history = [float(value) for value in range(500)]

    retained = retain_recent_gaps(history, 20)

    assert len(retained) == 20
    assert retained == [float(value) for value in range(480, 500)]
    assert retain_recent_gaps([1.0, 2.0]) == [1.0, 2.0], "short histories are untouched"


def test_gap_history_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        retain_recent_gaps([1.0], 0)


def test_a_bounded_history_keeps_the_window_adapting_to_a_new_cadence() -> None:
    """The reason the history is capped at all.

    The EWMA gain is ``2/(n+1)``, so an unbounded history drives it toward zero
    and the window freezes: an incident that fired every 100ms for hours and
    then slows to one alert every 10s should widen its window, and with 500
    gaps behind it, it barely moves. Bounding n floors the gain, so the same
    cadence change is actually tracked.
    """

    settled = [100.0] * 500
    slow_gap = 10_000.0

    unbounded = calculate_quiet_deadline(settled, slow_gap, 0)
    bounded = calculate_quiet_deadline(retain_recent_gaps(settled, 20), slow_gap, 0)

    assert unbounded["mean_gap"] < 200.0, "gain has decayed; the change is ignored"
    assert bounded["mean_gap"] > unbounded["mean_gap"] * 4
    assert bounded["quiet_at_ms"] > unbounded["quiet_at_ms"]


def test_the_default_gap_history_window_is_the_one_the_engine_uses() -> None:
    """The pure module's default and the configured limit must not drift."""

    assert settings.GAP_HISTORY_MAX == DEFAULT_GAP_HISTORY_MAX


@pytest.mark.parametrize(
    ("service", "alertname", "message", "reason"),
    [
        ("payment-api", "PaymentFailureRate", "payment capture failures", "PAYMENT_FAILURE"),
        ("auth-service", "AuthenticationOutage", "authentication unavailable", "AUTH_OUTAGE"),
        ("storage-service", "DataLossDetected", "data loss detected", "DATA_LOSS"),
    ],
)
def test_hardcoded_protected_emergencies_bypass_filters(
    service: str, alertname: str, message: str, reason: str
) -> None:
    event = _normalized_event(
        fired_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        service=service,
        alertname=alertname,
        message=message,
    )
    decision = classify_protected_critical(event)

    assert decision.should_bypass is True
    assert decision.reason == reason


def test_one_auth_failure_is_not_an_outage_and_stays_consolidated() -> None:
    """AUTH_OUTAGE must mean an outage.

    `auth` paired with any failure word used to bypass, so a routine
    `AuthTokenRefreshFailure` skipped dedupe and paged per alert - twelve
    identical alerts became twelve incidents. That is the alert fatigue this
    system exists to remove, manufactured by the rule meant to protect against
    it. The consolidated path still delivers these; it just does not page for
    every one.
    """

    event = _normalized_event(
        fired_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        service="auth-service",
        alertname="AuthTokenRefreshFailure",
        message="token refresh is failing for a rising share of sessions",
        pod="auth-a",
        extra_labels={"cluster": "core"},
    )

    assert classify_protected_critical(event).should_bypass is False


def test_a_genuine_auth_outage_still_bypasses() -> None:
    """The narrowing must not cost the case the rule exists for."""

    event = _normalized_event(
        fired_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        service="auth-service",
        alertname="AuthServiceDown",
        message="login is unavailable for all users",
        pod="auth-a",
        extra_labels={"cluster": "core"},
    )
    decision = classify_protected_critical(event)

    assert decision.should_bypass is True
    assert decision.reason == "AUTH_OUTAGE"


@pytest.mark.parametrize(
    ("alertname", "message"),
    [
        # `down` inside `downstream`, with an auth subject present.
        ("UpstreamTimeout", "downstream auth dependency is slow"),
        # `data` inside `metadata`, with a destructive state word present.
        ("MetadataSyncLag", "metadata rows were deleted by compaction"),
        # `auth` inside `oauth`, with an outage word present.
        ("SessionStoreLag", "oauth session store unavailable"),
    ],
)
def test_words_that_merely_contain_a_keyword_do_not_bypass(
    alertname: str, message: str
) -> None:
    """Substring matching made every one of these a protected emergency."""

    event = _normalized_event(
        fired_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        service="orders-api",
        alertname=alertname,
        message=message,
        pod="orders-a",
        extra_labels={"cluster": "core"},
    )

    assert classify_protected_critical(event).should_bypass is False


def test_p0_bypass_generates_idempotent_delivery_and_audit_artifacts() -> None:
    event = _normalized_event(
        fired_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        extra_labels={"priority": "P0"},
    )
    incident_id = uuid4()
    decision = classify_protected_critical(event)
    intents, audit_entry = build_bypass_artifacts(event, incident_id, decision)

    assert decision.should_bypass is True
    assert decision.reason == "PRIORITY_P0"
    assert {intent.provider for intent in intents} == {"pagerduty", "slack"}
    assert len({intent.idempotency_key for intent in intents}) == 2
    assert audit_entry.event_id == event.event_id
    assert audit_entry.incident_id == incident_id
    assert audit_entry.decision == "CRITICAL_BYPASS"
