import json
from pathlib import Path

from src.ingest.prometheus import normalize_prometheus
from src.utils.fingerprint import compute_fingerprint

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "prometheus_sample.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_normalize_prometheus_produces_expected_event_count():
    payload = _load_fixture()
    events = normalize_prometheus(payload)
    assert len(events) == len(payload["alerts"])


def test_normalize_prometheus_extracts_fields():
    payload = _load_fixture()
    events = normalize_prometheus(payload)

    first = events[0]
    assert first.service == "payment-api"
    assert first.alertname == "HighCPUUsage"
    assert first.severity_raw == "warning"
    assert first.status == "firing"
    assert first.message == "CPU usage above 90% for 5 minutes"


def test_severity_extraction_for_critical_alert():
    payload = _load_fixture()
    events = normalize_prometheus(payload)

    critical_events = [e for e in events if e.severity_raw == "critical"]
    assert len(critical_events) == 1
    assert critical_events[0].alertname == "ServiceDown"


def test_fingerprint_is_deterministic():
    labels = {"service": "payment-api", "alertname": "HighCPUUsage", "severity": "warning"}
    fp1 = compute_fingerprint("payment-api", "HighCPUUsage", labels)
    fp2 = compute_fingerprint("payment-api", "HighCPUUsage", labels)
    assert fp1 == fp2
    assert len(fp1) == 64


def test_fingerprint_is_order_independent_across_label_dicts():
    labels_a = {"b": "2", "a": "1"}
    labels_b = {"a": "1", "b": "2"}
    assert compute_fingerprint("svc", "alert", labels_a) == compute_fingerprint("svc", "alert", labels_b)


def test_fingerprint_differs_for_different_labels():
    fp1 = compute_fingerprint("svc", "alert", {"instance": "a"})
    fp2 = compute_fingerprint("svc", "alert", {"instance": "b"})
    assert fp1 != fp2
