"""Grafana webhook normalization, route validation, and engine integration tests."""

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
from src.ingest.normalize_grafana import normalize_grafana
import src.main as main


SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


@pytest.fixture
def grafana_payload() -> dict:
    # Shape matches the alert objects Grafana Alerting sends to a webhook contact point.
    return {
        "receiver": "pulsegraph-webhook",
        "status": "firing",
        "alerts": [
            {
                "state": "Alerting",
                "labels": {
                    "alertname": "Database connection pool exhausted",
                    "severity": "high",
                    "environment": "production",
                    "cluster": "payments-eks",
                    "service": "ledger-api",
                    "team": "payments",
                },
                "message": "All database connections are in use for ledger-api.",
                "startsAt": "2026-09-04T10:15:30.123Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "https://grafana.example.com/alerting/grafana/pool",
                "fingerprint": "f8a67db11c29ed24",
            }
        ],
        "groupLabels": {"alertname": "Database connection pool exhausted"},
        "commonLabels": {"environment": "production", "cluster": "payments-eks"},
        "externalURL": "https://grafana.example.com/",
        "version": "4",
        "groupKey": "{}:{alertname=\"Database connection pool exhausted\"}",
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


def test_normalize_grafana_maps_real_alert_fields(grafana_payload) -> None:
    event = normalize_grafana(grafana_payload)[0]

    assert event.source == "grafana"
    assert event.alertname == "Database connection pool exhausted"
    assert event.message == "All database connections are in use for ledger-api."
    assert event.status == "firing"
    assert event.severity_raw == "high"
    assert event.service == "ledger-api"
    assert event.labels["environment"] == "production"
    assert event.labels["cluster"] == "payments-eks"
    assert event.fired_at.isoformat() == "2026-09-04T10:15:30.123000+00:00"
    assert event.raw_payload["source_event_id"] == "f8a67db11c29ed24"


@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        ("Alerting", "firing"),
        ("OK", "resolved"),
        ("firing", "firing"),
        ("resolved", "resolved"),
    ],
)
def test_normalize_grafana_maps_alert_state(grafana_payload, state, expected_status) -> None:
    grafana_payload["alerts"][0]["state"] = state
    assert normalize_grafana(grafana_payload)[0].status == expected_status


def test_normalize_grafana_uses_real_alert_annotations_when_message_is_absent(
    grafana_payload,
) -> None:
    alert = grafana_payload["alerts"][0]
    alert.pop("message")
    alert["annotations"] = {"summary": "Connection pool saturation detected."}

    assert normalize_grafana(grafana_payload)[0].message == "Connection pool saturation detected."


def test_normalize_grafana_supports_its_status_field_when_state_is_omitted(
    grafana_payload,
) -> None:
    alert = grafana_payload["alerts"][0]
    alert.pop("state")
    alert["status"] = "resolved"

    assert normalize_grafana(grafana_payload)[0].status == "resolved"


def test_normalize_grafana_normalizes_the_env_label_for_scope(grafana_payload) -> None:
    labels = grafana_payload["alerts"][0]["labels"]
    labels.pop("environment")
    labels["env"] = "staging"

    event = normalize_grafana(grafana_payload)[0]
    assert event.labels["environment"] == "staging"


def test_normalize_grafana_defaults_missing_severity_to_info(grafana_payload) -> None:
    grafana_payload["alerts"][0]["labels"].pop("severity")
    assert normalize_grafana(grafana_payload)[0].severity_raw == "info"


def test_normalize_grafana_processes_every_alert_in_a_webhook(grafana_payload) -> None:
    second_alert = deepcopy(grafana_payload["alerts"][0])
    second_alert["labels"] = {
        **second_alert["labels"],
        "alertname": "Ledger API error budget exhausted",
    }
    second_alert["fingerprint"] = "b42dba0a5a58241d"
    grafana_payload["alerts"].append(second_alert)

    events = normalize_grafana(grafana_payload)
    assert [event.alertname for event in events] == [
        "Database connection pool exhausted",
        "Ledger API error budget exhausted",
    ]
    assert events[1].raw_payload["source_event_id"] == "b42dba0a5a58241d"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload.pop("alerts"),
        lambda payload: payload.update({"alerts": []}),
        lambda payload: payload["alerts"][0].pop("labels"),
        lambda payload: payload["alerts"][0]["labels"].pop("alertname"),
        lambda payload: payload["alerts"][0].update({"state": "Pending"}),
        lambda payload: payload["alerts"][0].pop("startsAt"),
        lambda payload: payload["alerts"][0].pop("fingerprint"),
    ],
)
def test_normalize_grafana_rejects_missing_or_invalid_required_fields(
    grafana_payload, mutator
) -> None:
    mutator(grafana_payload)
    with pytest.raises(ValueError):
        normalize_grafana(grafana_payload)


def test_grafana_route_processes_a_valid_payload(api_client, grafana_payload) -> None:
    response = api_client.post("/v1/ingest/grafana", json=grafana_payload)

    assert response.status_code == 200
    assert response.json()["ingested"] == 1
    assert response.json()["results"][0]["bypassed"] is False


def test_grafana_route_returns_422_for_invalid_payload(api_client) -> None:
    response = api_client.post("/v1/ingest/grafana", json={"alerts": []})

    assert response.status_code == 422
    assert "invalid grafana payload" in response.json()["detail"]


def test_grafana_route_returns_422_for_malformed_json(api_client) -> None:
    response = api_client.post(
        "/v1/ingest/grafana",
        content="{not json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_grafana_event_dedupes_in_its_environment_cluster_scope(
    engine_db, grafana_payload
) -> None:
    first = normalize_grafana(grafana_payload)[0]
    first_decision = await process_event(engine_db, first)
    await persist_decision(engine_db, first_decision)
    await engine_db.commit()

    retry_payload = deepcopy(grafana_payload)
    retry_payload["alerts"][0]["startsAt"] = "2026-09-04T10:16:30.123Z"
    retry = normalize_grafana(retry_payload)[0]
    retry_decision = await process_event(engine_db, retry)

    assert first_decision.scope_key == "production/payments-eks"
    assert retry_decision.incident_id == first_decision.incident_id
    assert retry_decision.is_duplicate is True
    assert retry_decision.state == "ACKNOWLEDGED"


@pytest.mark.asyncio
async def test_grafana_event_uses_default_scope_when_labels_are_absent(
    engine_db, grafana_payload
) -> None:
    labels = grafana_payload["alerts"][0]["labels"]
    labels.pop("environment")
    labels.pop("cluster")

    decision = await process_event(engine_db, normalize_grafana(grafana_payload)[0])

    assert decision.scope_key == "default/default"
