"""Acceptance criteria for PulseGraph's mathematical core and wrapper."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import pytest
from uuid import UUID, uuid4

from src.engine.adaptive_ewma import (
    DEFAULT_INITIAL_WINDOW_MS,
    calculate_quiet_deadline,
)
from src.engine.critical_bypass import (
    build_bypass_artifacts,
    classify_protected_critical,
)
from src.engine.dedupe import generate_fingerprint, is_exact_duplicate
from src.engine.incident_machine import transition_state
from src.engine.process_event import IncidentOutcome, IncidentSnapshot, process_event
from src.engine.timer_wheel import TimerWheel


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


@pytest.mark.parametrize(
    ("current_state", "trigger"),
    [
        ("OPEN", "RESOLVE"),
        ("ACKNOWLEDGED", "REOPEN"),
        ("QUIESCENT", "ACKNOWLEDGE"),
        ("RESOLVED", "ACKNOWLEDGE"),
        ("UNKNOWN", "ACKNOWLEDGE"),
        ("OPEN", "UNKNOWN"),
    ],
)
def test_machine_rejects_invalid_transitions(current_state: str, trigger: str) -> None:
    assert transition_state(current_state, trigger) is None


@dataclass(frozen=True)
class _NormalizedEvent:
    event_id: UUID
    fingerprint: str
    source: str
    service: str
    alertname: str
    severity_raw: str
    status: str
    labels: dict[str, str]
    message: str
    fired_at: datetime
    raw_payload: dict[str, object]


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
) -> _NormalizedEvent:
    labels = {
        "environment": "production",
        "cluster": "payments",
        "pod": pod,
        "pod_uid": f"uid-{pod}",
    }
    if extra_labels:
        labels.update(extra_labels)
    return _NormalizedEvent(
        event_id=uuid4(),
        fingerprint="ingest-fingerprint-is-not-the-engine-key",
        source="prometheus",
        service=service,
        alertname=alertname,
        severity_raw=severity_raw,
        status=status,
        labels=labels,
        message=message,
        fired_at=fired_at,
        raw_payload={"labels": labels},
    )


@dataclass
class _FakeTransaction:
    candidate: IncidentSnapshot | None = None
    lookup_calls: int = field(default=0, init=False)
    requested_scope: str | None = field(default=None, init=False)

    async def find_active_incident(
        self, *, scope_key: str, fingerprint: str
    ) -> IncidentSnapshot | None:
        self.lookup_calls += 1
        self.requested_scope = scope_key
        return self.candidate


def _incident_event_payload(event: _NormalizedEvent) -> dict[str, object]:
    return {
        "scope_key": "production/payments",
        "service": event.service,
        "alertname": event.alertname,
        "severity_raw": event.severity_raw,
        "status": event.status,
        "labels": event.labels,
    }


def test_process_event_coalesces_a_duplicate_without_writing() -> None:
    first_at = datetime(2026, 9, 4, tzinfo=timezone.utc)
    first = _normalized_event(fired_at=first_at, pod="payment-api-a")
    incident_id = uuid4()
    transaction = _FakeTransaction(
        candidate=IncidentSnapshot(
            incident_id=incident_id,
            state="OPEN",
            last_event=_incident_event_payload(first),
            last_seen_ms=int(first_at.timestamp() * 1000),
            gap_history=(100.0, 120.0),
            alert_count=2,
        )
    )
    retry = _normalized_event(
        fired_at=first_at + timedelta(milliseconds=100),
        pod="payment-api-b",
    )

    outcome = asyncio.run(process_event(transaction, retry))

    assert isinstance(outcome, IncidentOutcome)
    assert outcome.incident_id == incident_id
    assert outcome.is_duplicate is True
    assert outcome.is_critical_bypass is False
    assert outcome.new_state == "OPEN"
    assert outcome.quiet_at_ms is not None
    assert "DUPLICATE_COALESCED" in outcome.card_changes
    assert transaction.lookup_calls == 1
    assert transaction.requested_scope == "production/payments"


def test_process_event_creates_a_new_open_incident_without_transaction_writes() -> None:
    transaction = _FakeTransaction()
    event = _normalized_event(fired_at=datetime(2026, 9, 4, tzinfo=timezone.utc))

    outcome = asyncio.run(process_event(transaction, event))

    assert outcome.is_duplicate is False
    assert outcome.new_state == "OPEN"
    assert outcome.quiet_at_ms is not None
    assert "INCIDENT_OPENED" in outcome.card_changes
    assert transaction.lookup_calls == 1


def test_process_event_bypasses_lookup_and_filtering_for_protected_critical() -> None:
    transaction = _FakeTransaction()
    event = _normalized_event(
        fired_at=datetime(2026, 9, 4, tzinfo=timezone.utc), severity_raw="critical"
    )

    outcome = asyncio.run(process_event(transaction, event))

    assert outcome.is_critical_bypass is True
    assert outcome.is_duplicate is False
    assert outcome.new_state == "OPEN"
    assert outcome.quiet_at_ms is None
    assert outcome.card_changes == ("CRITICAL_BYPASS",)
    assert transaction.lookup_calls == 0
    assert {intent.provider for intent in outcome.delivery_intents} == {
        "pagerduty",
        "slack",
    }
    assert outcome.audit_entry is not None
    assert outcome.audit_entry.decision == "CRITICAL_BYPASS"


def test_process_event_reopens_a_resolved_duplicate() -> None:
    first_at = datetime(2026, 9, 4, tzinfo=timezone.utc)
    previous = _normalized_event(fired_at=first_at)
    transaction = _FakeTransaction(
        candidate=IncidentSnapshot(
            incident_id=uuid4(),
            state="RESOLVED",
            last_event=_incident_event_payload(previous),
            last_seen_ms=int(first_at.timestamp() * 1000),
            gap_history=(),
            alert_count=1,
        )
    )
    firing_again = _normalized_event(fired_at=first_at + timedelta(seconds=1))

    outcome = asyncio.run(process_event(transaction, firing_again))

    assert outcome.new_state == "OPEN"
    assert "STATE_RESOLVED_TO_OPEN" in outcome.card_changes


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
