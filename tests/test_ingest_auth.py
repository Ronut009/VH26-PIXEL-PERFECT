"""Ingest is a trust boundary: who may write, and what may they write."""

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.ingest.auth import (
    IngestAuthError,
    IngestCredential,
    IngestNotConfigured,
    IngestScopeError,
    authenticate,
    authorize_scope,
    parse_tokens,
)
from src.main import app
from tests.conftest import INGEST_HEADERS, INGEST_TOKEN


def _payload(environment: str = "production", status: str = "firing") -> dict:
    return {
        "receiver": "pulsegraph",
        "status": status,
        "alerts": [
            {
                "status": status,
                "fingerprint": "abc123",
                "labels": {
                    "alertname": "CheckoutErrorRateHigh",
                    "service": "checkout-api",
                    "severity": "critical",
                    "environment": environment,
                    "cluster": "eu-west",
                },
                "annotations": {"summary": "checkout 5xx rate rose"},
                "startsAt": "2026-09-04T10:15:30Z",
            }
        ],
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_PATH", str(tmp_path / "auth.db"))
    with TestClient(app) as test_client:
        yield test_client


# ── parsing credentials ───────────────────────────────────────────────────


def test_credentials_parse_with_and_without_a_scope():
    credentials = parse_tokens("prod-am:tok1:production/*, staging-am:tok2")

    assert [c.name for c in credentials] == ["prod-am", "staging-am"]
    assert credentials[0].scope == "production/*"
    assert credentials[1].scope == "*", "an omitted scope means unrestricted"


def test_a_malformed_entry_does_not_take_the_other_sources_offline():
    """One typo must not stop alert ingestion for every other source."""

    credentials = parse_tokens("good:tok1:production/*, broken, :nokey, alsogood:tok2")

    assert [c.name for c in credentials] == ["good", "alsogood"]


# ── who are you ───────────────────────────────────────────────────────────


def test_authentication_accepts_either_header_form():
    credentials = parse_tokens("src:s3cr3t")

    assert authenticate(credentials, "Bearer s3cr3t", None).name == "src"
    # Alertmanager can set Authorization; other senders find a plain header
    # easier, and both are equally verifiable.
    assert authenticate(credentials, None, "s3cr3t").name == "src"


def test_authentication_rejects_a_wrong_or_missing_credential():
    credentials = parse_tokens("src:s3cr3t")

    with pytest.raises(IngestAuthError):
        authenticate(credentials, "Bearer wrong", None)
    with pytest.raises(IngestAuthError):
        authenticate(credentials, None, None)
    with pytest.raises(IngestAuthError):
        authenticate(credentials, "Basic s3cr3t", None)


def test_no_configured_credentials_fails_closed():
    """An unauthenticated alerting system is worse than a loudly broken one."""

    with pytest.raises(IngestNotConfigured):
        authenticate((), "Bearer anything", None)


# ── what may you write ────────────────────────────────────────────────────


def test_a_scoped_credential_cannot_write_another_environment():
    staging = IngestCredential("staging-am", "tok", "staging/*")

    authorize_scope(staging, "staging/eu-west")
    with pytest.raises(IngestScopeError):
        authorize_scope(staging, "production/eu-west")

    # A prefix must not match a different environment that merely starts the
    # same way, so scope is compared including its separator.
    with pytest.raises(IngestScopeError):
        authorize_scope(IngestCredential("p", "t", "prod/"), "production/eu-west")


def test_a_wildcard_credential_may_write_anything():
    everything = IngestCredential("admin", "tok", "*")
    authorize_scope(everything, "production/eu-west")
    authorize_scope(everything, "staging/local")


# ── the attack this exists to stop ────────────────────────────────────────


def test_an_unauthenticated_request_cannot_create_an_incident(client):
    response = client.post("/v1/ingest/prometheus", json=_payload())

    assert response.status_code == 401
    assert "invalid ingest credential" in response.json()["detail"]


def test_an_unauthenticated_request_cannot_forge_a_resolve(client):
    """The attack: close a real firing incident with one anonymous request.

    Dedupe keys on stable labels, so a forged `resolved` whose labels match a
    live incident would land on that incident and silence it. Authentication is
    what makes this unreachable.
    """

    opened = client.post(
        "/v1/ingest/prometheus", json=_payload(), headers=INGEST_HEADERS
    )
    assert opened.status_code == 200

    forged = client.post("/v1/ingest/prometheus", json=_payload(status="resolved"))
    assert forged.status_code == 401

    recent = client.get("/v1/incidents/recent").json()
    states = [incident["status"] for incident in recent["incidents"]]
    assert "RESOLVED" not in states, "a forged resolve must not close a live incident"


def test_a_staging_token_cannot_silence_production(client, monkeypatch):
    """A leaked staging credential must not reach production incidents."""

    monkeypatch.setattr(settings, "INGEST_TOKENS", "staging-am:staging-tok:staging/*")
    app.state.ingest_credentials = parse_tokens(settings.INGEST_TOKENS)
    staging_headers = {"Authorization": "Bearer staging-tok"}

    allowed = client.post(
        "/v1/ingest/prometheus",
        json=_payload(environment="staging"),
        headers=staging_headers,
    )
    assert allowed.status_code == 200

    denied = client.post(
        "/v1/ingest/prometheus",
        json=_payload(environment="production"),
        headers=staging_headers,
    )
    assert denied.status_code == 403
    assert "scope not permitted" in denied.json()["detail"]


def test_a_batch_with_one_out_of_scope_alert_writes_nothing(client, monkeypatch):
    """Authorise the whole batch before writing, so it cannot half-apply."""

    monkeypatch.setattr(settings, "INGEST_TOKENS", "staging-am:staging-tok:staging/*")
    app.state.ingest_credentials = parse_tokens(settings.INGEST_TOKENS)

    mixed = _payload(environment="staging")
    smuggled = _payload(environment="production")["alerts"][0]
    smuggled["fingerprint"] = "def456"
    mixed["alerts"].append(smuggled)

    response = client.post(
        "/v1/ingest/prometheus",
        json=mixed,
        headers={"Authorization": "Bearer staging-tok"},
    )

    assert response.status_code == 403
    recent = client.get("/v1/incidents/recent").json()
    assert recent["incidents"] == [], "no partial write from a rejected batch"


def test_a_valid_credential_still_ingests_normally(client):
    response = client.post(
        "/v1/ingest/prometheus", json=_payload(), headers=INGEST_HEADERS
    )

    assert response.status_code == 200
    assert response.json()["ingested"] == 1


def test_the_plain_token_header_works_end_to_end(client):
    response = client.post(
        "/v1/ingest/prometheus",
        json=_payload(),
        headers={"X-PulseGraph-Token": INGEST_TOKEN},
    )

    assert response.status_code == 200
