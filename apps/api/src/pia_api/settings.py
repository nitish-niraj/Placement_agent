"""Application settings (pydantic-settings), read from environment / infrastructure/.env.

SEC-001: secrets arrive via env only — never hardcode them here.
SEC-005: ACTION_AUTOMATION_ENABLED=true is refused at construction time in MVP builds.
"""

import os
from urllib.parse import quote

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _database_url_from_postgres(data: dict) -> dict:
    """Compose DATABASE_URL from POSTGRES_* when unset (host-side dev/tests).
    Explicit DATABASE_URL always wins; without POSTGRES_USER the localhost
    default stands. Same rule as the worker settings."""
    if not isinstance(data, dict):
        return data
    if (data.get("DATABASE_URL") or data.get("database_url")
            or os.environ.get("DATABASE_URL")):
        return data
    user = os.environ.get("POSTGRES_USER")
    if not user:
        return data
    password = quote(os.environ.get("POSTGRES_PASSWORD", "pia"))
    db = os.environ.get("POSTGRES_DB", "pia")
    # Lowercase field name: init-kwarg population is case-sensitive and the
    # model ignores unknown (uppercase) extras.
    return {**data,
            "database_url": f"postgresql+psycopg://{quote(user)}:{password}"
                            f"@localhost:5432/{db}"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "pia-api"
    app_version: str = "0.1.0"
    environment: str = "development"
    app_timezone: str = "Asia/Kolkata"

    # Infrastructure (compose defaults match infrastructure/env.example)
    database_url: str = "postgresql+psycopg://pia:pia@localhost:5432/pia"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "http://localhost:9000"
    minio_bucket: str = "pia-media"

    # Dashboard auth (single-user token; Q3 default per 00_Master_Plan §7)
    dashboard_token: str = "change-me"
    require_auth: bool = True

    # Evolution API connector — configured in P1; empty = not deployed yet
    evolution_base_url: str = ""
    # SEC-003: webhook secret the connector must send as `X-PIA-Token`. Fail-closed:
    # when empty, the webhook endpoint refuses everything (503).
    evolution_webhook_secret: str = ""
    webhook_rate_limit_per_minute: int = 600  # history-sync bursts are legitimate
    # Round 2 throughput: per-group burst shaping window + budget. Past the
    # budget, message jobs get scheduled delays (never dropped).
    webhook_burst_per_group: int = 120
    webhook_burst_window_seconds: int = 300

    # Policy
    media_max_size_mb: int = 25
    attachment_analysis_enabled: bool = True  # False = skip download enqueue
    raw_message_retention_days: int = 90  # §22 / 02_TRD §11 (Q7 default)
    document_retention_days: int = 365  # attachments/media lifecycle (SEC-007)
    # P9 dedup windows + threshold (measured on the live corpus — see
    # docs/08_P9_Dedup_Evaluation.md; ADR-009 discipline: never invented)
    dedup_near_window_hours: int = 48
    dedup_exact_reminder_days: int = 7
    dedup_near_threshold: float = 0.72
    # SEC-002: 32-byte urlsafe-base64 key for encrypting sensitive profile fields.
    # Empty = plaintext mode (local dev only) — set before any VPS deployment.
    pia_encryption_key: str = ""
    action_automation_enabled: bool = False  # SEC-005 kill switch
    # P14.1 per-capability switch (ADR-012): approve enqueues the form
    # submit job only when this is on (FORM_SUBMIT_DRY_RUN still applies).
    form_automation_enabled: bool = False
    stage3_executors_enabled: bool = False  # ADR-013 per-capability switch

    @model_validator(mode="before")
    @classmethod
    def _compose_database_url(cls, data):
        return _database_url_from_postgres(data)

    @model_validator(mode="after")
    def enforce_action_kill_switch(self) -> "Settings":
        if self.action_automation_enabled:
            raise ValueError(
                "ACTION_AUTOMATION_ENABLED=true is refused in MVP builds (SEC-005). "
                "Irreversible external actions require an ADR and an approval workflow first."
            )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
