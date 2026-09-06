"""Shared pytest fixtures."""

import pytest

from pia_api.settings import Settings


@pytest.fixture
def api_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Deterministic Settings instance for tests (no .env file)."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    monkeypatch.setenv("REQUIRE_AUTH", "true")
    monkeypatch.setenv("EVOLUTION_BASE_URL", "")
    return Settings(_env_file=None)  # type: ignore[call-arg]
