"""One judge-friendly executable proof of the PulseGraph MVP.

Run from the repository root:
    .venv\\Scripts\\python.exe -m pytest tests\\test_pulsegraph_mvp.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import src.main as main
from src.contracts import NormalizedEvent
from src.db.writer import DbWriter


SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    database_path = tmp_path / "alerts.db"
    monkeypatch.setattr(main.settings, "DATABASE_PATH", str(database_path))
    with TestClient(main.app) as client:
        yield client


@pytest_asyncio.fixture
async def database() -> aiosqlite.Connection:
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield connection
    await connection.close()


def _event(*, event_id=None, fired_at=None, severity="warning", pod="checkout-a") -> NormalizedEvent:
    timestamp = fired_at or datetime(2026, 9, 4, tzinfo=timezone.utc)
    labels = {
        "environment": "demo",
        "cluster": "jury-cluster",
        "service": "checkout-api",
        "pod": pod,
        "pod_uid": f"uid-{pod}",
    }
    return NormalizedEvent(
        event_id=event_id or uuid4(),
        fingerprint="demo-ingest-fingerprint",
        source="prometheus",
        service="checkout-api",
        alertname="CheckoutLatencyHigh",
        severity_raw=severity,
        status="firing",
        labels=labels,
        message="Checkout latency above the service objective",
        fired_at=timestamp,
        raw_payload={"labels": labels},
    )


def test_mvp_accepts_prometheus_datadog_and_grafana(api_client) -> None:
    """All three monitoring providers normalize into the same event pipeline."""
    prometheus = {
        "alerts": [{
            "status": "firing",
            "labels": {"alertname": "CheckoutLatencyHigh", "service": "checkout-api", "severity": "warning", "environment": "demo", "cluster": "jury-cluster"},
            "annotations": {"summary": "Checkout latency is elevated"},
            "startsAt": "2026-09-04T10:00:00Z",
        }]
    }
    datadog = {
        "event": {
            "id": 9001,
            "title": "Checkout API error rate high",
            "text": "Checkout requests are failing above the error budget.",
            "alert_transition": "Triggered",
            "priority": "P2",
            "tags": ["env:demo", "cluster:jury-cluster", "service:checkout-api"],
            "timestamp": "2026-09-04T10:01:00Z",
        }
    }
    grafana = {
        "receiver": "pulsegraph",
        "alerts": [{
            "state": "Alerting",
            "labels": {"alertname": "Ledger pool exhausted", "severity": "high", "environment": "demo", "cluster": "jury-cluster", "service": "ledger-api"},
            "message": "All database connections are in use.",
            "startsAt": "2026-09-04T10:02:00Z",
            "fingerprint": "grafana-jury-001",
        }],
    }

    for path, payload in (
        ("/v1/ingest/prometheus", prometheus),
        ("/v1/ingest/datadog", datadog),
        ("/v1/ingest/grafana", grafana),
    ):
        response = api_client.post(path, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["ingested"] == 1


@pytest.mark.asyncio
async def test_mvp_many_duplicate_alerts_become_one_incident(database) -> None:
    """The central product promise: duplicate alerts coalesce and remain auditable."""
    writer = DbWriter()
    first = await writer.process_event(database, _event(pod="checkout-a"))
    duplicate = await writer.process_event(
        database,
        _event(pod="checkout-b", fired_at=datetime(2026, 9, 4, tzinfo=timezone.utc) + timedelta(seconds=1)),
    )

    assert duplicate["incident_id"] == first["incident_id"]
    assert duplicate["is_duplicate"] is True
    async with database.execute("SELECT prev_hash, row_hash FROM raw_events ORDER BY seq") as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 2
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]


@pytest.mark.asyncio
async def test_mvp_critical_alert_bypasses_batching(database) -> None:
    """Protected emergencies go immediately to PagerDuty and Slack."""
    critical = _event(severity="critical")
    result = await DbWriter().process_event(database, critical)

    assert result["bypassed"] is True
    async with database.execute(
        "SELECT channel, action FROM delivery_intents WHERE incident_id = ?", (result["incident_id"],)
    ) as cursor:
        deliveries = await cursor.fetchall()
    assert {(row["channel"], row["action"]) for row in deliveries} == {
        ("pagerduty", "trigger"),
        ("slack", "create"),
    }


def test_mvp_rejects_invalid_provider_payloads(api_client) -> None:
    """Bad webhook data is rejected instead of corrupting the incident pipeline."""
    assert api_client.post("/v1/ingest/prometheus", json={"alerts": [{}]}).status_code == 400
    assert api_client.post("/v1/ingest/datadog", json={"event": {"title": "missing fields"}}).status_code == 422
    assert api_client.post("/v1/ingest/grafana", json={"alerts": []}).status_code == 422
