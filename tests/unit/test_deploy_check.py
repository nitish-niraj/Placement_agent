"""SEC-001 boot gate: placeholder secrets refuse to boot; dev plaintext-PII
mode stays allowed locally but never in production."""

import pytest

from pia_api.main import _enforce_secrets
from pia_shared.deploy import find_secret_problems


class TestDeployCheck:
    def test_placeholder_token_refused(self) -> None:
        for bad in ("", "change-me", "  change-me  "):
            assert find_secret_problems(
                dashboard_token=bad, environment="development",
                pia_encryption_key="k") == [
                    "DASHBOARD_TOKEN is missing or still the placeholder"]

    def test_good_settings_pass(self) -> None:
        assert find_secret_problems(
            dashboard_token="pia-abc123", environment="development",
            pia_encryption_key="") == []
        assert find_secret_problems(
            dashboard_token="pia-abc123", environment="production",
            pia_encryption_key="k") == []

    def test_production_plaintext_pii_refused(self) -> None:
        assert find_secret_problems(
            dashboard_token="pia-abc123", environment="production",
            pia_encryption_key="") == [
                "PIA_ENCRYPTION_KEY is empty outside development "
                "(SEC-002 forbids plaintext PII mode)"]

    def test_worker_skips_token_check(self) -> None:
        assert find_secret_problems(
            dashboard_token=None, environment="development",
            pia_encryption_key="") == []


class TestServeGate:
    @staticmethod
    def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("DASHBOARD_TOKEN", "PIA_ENCRYPTION_KEY", "ENVIRONMENT"):
            monkeypatch.delenv(var, raising=False)

    def test_placeholders_refuse_to_serve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pia_api.settings import Settings

        self._clean_env(monkeypatch)
        monkeypatch.setattr(
            "pia_api.settings._settings",
            Settings(_env_file=None),  # type: ignore[call-arg]
        )  # dashboard_token defaults to the "change-me" placeholder
        with pytest.raises(RuntimeError, match="refusing to serve"):
            _enforce_secrets()

    def test_valid_settings_serve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pia_api.settings import Settings

        self._clean_env(monkeypatch)
        monkeypatch.setattr(
            "pia_api.settings._settings",
            Settings(_env_file=None, dashboard_token="t"),  # type: ignore[call-arg]
        )
        assert _enforce_secrets() is None
