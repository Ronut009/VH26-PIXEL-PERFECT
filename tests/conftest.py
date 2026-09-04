"""Shared test setup.

Ingest authentication is enabled by default and fails closed, so every test
that posts an alert needs a credential. Configuring one here - rather than
disabling auth for the whole suite - keeps the route tests exercising the same
authenticated path production uses.
"""

import pytest

from src.config import settings

INGEST_TOKEN = "test-ingest-token"
INGEST_SOURCE = "test-source"
INGEST_HEADERS = {"Authorization": f"Bearer {INGEST_TOKEN}"}


@pytest.fixture(autouse=True)
def configured_ingest_credential():
    """Give the suite one wildcard-scoped ingest credential."""

    previous_tokens = settings.INGEST_TOKENS
    previous_enabled = settings.INGEST_AUTH_ENABLED
    settings.INGEST_TOKENS = f"{INGEST_SOURCE}:{INGEST_TOKEN}:*"
    settings.INGEST_AUTH_ENABLED = True
    try:
        yield
    finally:
        settings.INGEST_TOKENS = previous_tokens
        settings.INGEST_AUTH_ENABLED = previous_enabled
