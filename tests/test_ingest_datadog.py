"""Datadog webhook normalization, route validation, and engine integration tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3

import aiosqlite
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from src.engine.db_adapter import persist_decision
from src.engine.process_event import process_event
from src.ingest.normalize_datadog import normalize_datadog
import src.main as main


SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


@pytest.fixture
def datadog_payload() -> dict:
    # Shape matches Datadog event-webhook fields, including its tag list.
    return {
        "event": {
            "id": 9876543210,
            "title": "Checkout API latency above SLO",
            "text": "p95 latency has exceeded 900ms for the past five minutes.",
            "alert_transition": "Triggered",
            "priority": "P1",
            "tags": [
                "env:production",
                "cluster:checkout-eks",
                "service:checkout-api",
                "team:payments",
                "region:us-east-1",
            ],
            "timestamp": 1_788_516_000,
            "alert_type": "metric alert",
        }
    }


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    database_path = tmp_path / "alerts.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    monkeypatch.setattr(main.settings, "DATABASE_PATH", str(database_path))

    with TestClient(main.app) as client:
        yield client


@pytest_asyncio.fixture
async def engine_db() -> aiosqlite.Connection:
    connection = await aiosqlite.connect(":memory:")
    connection.row_factory = aiosqlite.Row
    await connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield connection
    await connection.close()


def test_normalize_datadog_maps_real_event_fields(datadog_payload) -> None:
    event = normalize_datadog(datadog_payload)[0]

    assert event.source == "datadog"
    assert event.alertname == "Checkout API latency above SLO"
    assert event.message == "p95 latency has exceeded 900ms for the past five minutes."
    assert event.status == "firing"
    assert event.severity_raw == "critical"
    assert event.service == "checkout-api"
    assert event.labels["environment"] == "production"
    assert event.labels["cluster"] == "checkout-eks"
    assert event.fired_at.isoformat() == "2026-09-04T10:00:00+00:00"
    assert event.raw_payload["source_event_id"] == "9876543210"


@pytest.mark.parametrize(
    ("priority", "expected_severity"),
    [("P1", "critical"), ("P2", "high"), ("P3", "medium"), ("P4", "low")],
)
def test_normalize_datadog_maps_provider_priorities(
    datadog_payload, priority: str, expected_severity: str
) -> None:
    datadog_payload["event"]["priority"] = priority
    assert normalize_datadog(datadog_payload)[0].severity_raw == expected_severity


def test_normalize_datadog_maps_recovered_to_resolved(datadog_payload) -> None:
    datadog_payload["event"]["alert_transition"] = "Recovered"
    assert normalize_datadog(datadog_payload)[0].status == "resolved"


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (1_788_516_000, "2026-09-04T10:00:00+00:00"),
        (1_788_516_000_000, "2026-09-04T10:00:00+00:00"),
        ("2026-09-04T10:00:00Z", "2026-09-04T10:00:00+00:00"),
    ],
)
def test_normalize_datadog_accepts_real_timestamp_encodings(
    datadog_payload, timestamp, expected
) -> None:
    datadog_payload["event"]["timestamp"] = timestamp
    assert normalize_datadog(datadog_payload)[0].fired_at.isoformat() == expected


def test_normalize_datadog_preserves_explicit_environment_tag(datadog_payload) -> None:
    datadog_payload["event"]["tags"].extend(["env:staging", "environment:production"])
    event = normalize_datadog(datadog_payload)[0]

    assert event.labels["env"] == "staging"
    assert event.labels["environment"] == "production"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload.pop("event"),
        lambda payload: payload["event"].pop("title"),
        lambda payload: payload["event"].update({"alert_transition": "Unknown"}),
        lambda payload: payload["event"].update({"tags": {"service": "api"}}),
        lambda payload: payload["event"].pop("timestamp"),
        lambda payload: payload["event"].pop("id"),
    ],
)
def test_normalize_datadog_rejects_missing_or_invalid_required_fields(
    datadog_payload, mutator
) -> None:
    mutator(datadog_payload)
    with pytest.raises(ValueError):
        normalize_datadog(datadog_payload)


def test_datadog_route_processes_a_valid_payload(api_client, datadog_payload) -> None:
    response = api_client.post("/v1/ingest/datadog", json=datadog_payload)

    assert response.status_code == 200
    assert response.json()["ingested"] == 1
    assert response.json()["results"][0]["bypassed"] is True


def test_datadog_route_returns_422_for_invalid_payload(api_client) -> None:
    response = api_client.post("/v1/ingest/datadog", json={"event": {"title": "missing"}})

    assert response.status_code == 422
    assert "invalid datadog payload" in response.json()["detail"]


def test_datadog_route_returns_422_for_malformed_json(api_client) -> None:
    response = api_client.post(
        "/v1/ingest/datadog",
        content="{not json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_datadog_event_dedupes_in_its_environment_cluster_scope(
    engine_db, datadog_payload
) -> None:
    datadog_payload["event"]["priority"] = "P2"
    first = normalize_datadog(datadog_payload)[0]
    first_decision = await process_event(engine_db, first)
    await persist_decision(engine_db, first_decision)
    await engine_db.commit()

    retry_payload = deepcopy(datadog_payload)
    retry_payload["event"]["timestamp"] += 30
    retry = normalize_datadog(retry_payload)[0]
    retry_decision = await process_event(engine_db, retry)

    assert first_decision.scope_key == "production/checkout-eks"
    assert retry_decision.incident_id == first_decision.incident_id
    assert retry_decision.is_duplicate is True
    assert retry_decision.state == "ACKNOWLEDGED"


@pytest.mark.asyncio
async def test_datadog_event_uses_default_scope_when_scope_tags_are_absent(
    engine_db, datadog_payload
) -> None:
    datadog_payload["event"]["priority"] = "P2"
    datadog_payload["event"]["tags"] = ["service:checkout-api", "team:payments"]

    decision = await process_event(engine_db, normalize_datadog(datadog_payload)[0])

    assert decision.scope_key == "default/default"
