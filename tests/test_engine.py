"""Acceptance-test skeletons for the pure PulseGraph incident engine.

These tests deliberately remain skipped until Phase 2 defines the exact
acceptance cases. They make the Phase 1 contract visible without implying that
the engine implementation exists yet.
"""

import pytest


@pytest.mark.skip(reason="Implementation pending")
def test_dedupe() -> None:
    """Exact fingerprints match only within the intended alert scope."""


@pytest.mark.skip(reason="Implementation pending")
def test_ewma() -> None:
    """Gap history produces a deterministic quiet-deadline prediction."""


@pytest.mark.skip(reason="Implementation pending")
def test_machine() -> None:
    """Lifecycle transitions accept valid triggers and reject invalid ones."""

