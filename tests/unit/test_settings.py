"""Settings behavior: env loading and the SEC-005 kill switch."""

import pytest
from pydantic import ValidationError

from pia_api.settings import Settings
from pia_worker.settings import WorkerSettings


def test_defaults_are_safe(api_settings: Settings) -> None:
    assert api_settings.action_automation_enabled is False
    assert api_settings.app_timezone == "Asia/Kolkata"
    assert api_settings.require_auth is True
    assert api_settings.media_max_size_mb == 25


def test_kill_switch_refuses_true_in_mvp() -> None:
    """SEC-005: config parse must refuse ACTION_AUTOMATION_ENABLED=true in MVP builds."""
    with pytest.raises(ValidationError, match="SEC-005"):
        Settings(_env_file=None, action_automation_enabled=True)  # type: ignore[call-arg]


def test_kill_switch_worker_refuses_true() -> None:
    with pytest.raises(ValidationError, match="SEC-005"):
        WorkerSettings(_env_file=None, action_automation_enabled=True)  # type: ignore[call-arg]


def test_token_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-from-env")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.dashboard_token == "secret-from-env"


def test_worker_settings_defaults() -> None:
    settings = WorkerSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.default_job_retries == 5
