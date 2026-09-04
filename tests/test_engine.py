"""Acceptance criteria for PulseGraph's pure mathematical engine."""

import pytest

from src.engine.adaptive_ewma import calculate_quiet_deadline
from src.engine.dedupe import generate_fingerprint, is_exact_duplicate
from src.engine.incident_machine import transition_state


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


def test_ewma_handles_zero_gap_and_rejects_negative_gap() -> None:
    assert calculate_quiet_deadline([0.0, 0.0], 0.0, 100) == {
        "quiet_at_ms": 100,
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
