"""Worker settings (F-003: queue abstraction, retries, dead-letter queue)."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    redis_url: str = "redis://localhost:6379/0"
    heartbeat_interval_seconds: int = 10
    default_job_retries: int = 5
    maintenance_interval_hours: int = 24
    deadline_sweep_interval_minutes: int = 60  # DUE_SOON/EXPIRED precision (F-020)
    # Teams listener (DEC-008 amendment): login popup credentials
    teams_email: str = ""
    teams_password: str = ""
    app_timezone: str = "Asia/Kolkata"  # master §22: all timestamps stored UTC, resolved here

    # Job targets (P2: persistence, media, retention)
    database_url: str = "postgresql+psycopg://pia:pia@localhost:5432/pia"
    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "piaminio"
    minio_secret_key: str = "piaminio-secret"
    minio_bucket: str = "pia-media"
    evolution_base_url: str = "http://localhost:8080"
    evolution_instance_name: str = "pia"
    evolution_api_key: str = ""
    media_max_size_mb: int = 25
    raw_message_retention_days: int = 90
    document_retention_days: int = 365  # media/attachments (SEC-007)
    vision_confidence_floor: float = 0.75  # provisional; tuned in P6 (ADR-009)

    # Identity matching thresholds (TRD §6, ADR-009): tuned on the P6 fixture
    # corpus, NOT invented. Corpus separation: label "me" >= 0.957, "could_be_me"
    # >= 0.917, "not_me" <= 0.870 — FUZZY=0.95 sits under every true-variant
    # pair, AMBIGUOUS=0.90 sits inside the 0.870..0.917 gap. Rationale + error
    # trade-off: docs/07_P6_Threshold_Evaluation.md.
    fuzzy_match_threshold: float = 0.95
    ambiguous_match_threshold: float = 0.90

    # SEC-002: decrypts sensitive profile fields for the matching ladder
    # (registration number is THE key identifier). Same key as the API.
    pia_encryption_key: str = ""

    # LLM: NVIDIA NIM via OpenAI-compatible adapter (DEC-006, DEC-010).
    # Primary text model swapped 2026-09-13: mistral-nemotron timed out on
    # live summary calls; nemotron-3-super-120b won the mini bake-off
    # (schema-valid JSON in 4.3 s, free tier).
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_model: str = "nvidia/nemotron-3-super-120b-a12b"
    llm_vision_model: str = "meta/llama-3.2-11b-vision-instruct"
    embedding_model: str = "nvidia/nemotron-3-embed-1b"  # 2048 dims = message_embeddings
    llm_timeout_seconds: int = 45
    ask_retrieve_k: int = 8  # P12 conversational search context size

    # Summary fallback chain (owner decisions 2026-09-12/13, DEC-010):
    # NIM structured -> OpenRouter -> Groq -> raw transcript. All keys live
    # ONLY in infrastructure/.env (SEC-001). OpenRouter default is the
    # free-tier reasoning model (needs token headroom; manual-tested
    # 2026-09-13); Groq default is the free-tier gpt-oss-120b (1K req +
    # 200K tokens/day free, tested 2026-09-13).
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "inclusionai/ling-3.0-flash-vl:free"
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "openai/gpt-oss-120b"

    # SEC-005 kill switch — same refusal rule as the API.
    action_automation_enabled: bool = False

    # P10 notifications (DEC-002: Telegram primary channel)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    digest_hour: int = 20  # 20:30 Asia/Kolkata default (TRD §8)
    digest_minute: int = 30
    notify_send_attempts: int = 3  # RQ retries before PENDING_DELIVERY
    # master §11 policy flags (wire-up completed in the fixed-code backlog)
    notify_critical_immediately: bool = True
    notify_medium_in_digest: bool = True

    @model_validator(mode="after")
    def enforce_action_kill_switch(self) -> "Settings":
        if self.action_automation_enabled:
            raise ValueError(
                "ACTION_AUTOMATION_ENABLED=true is refused in MVP builds (SEC-005)."
            )
        return self


@lru_cache
def get_settings() -> "Settings":
    return Settings()


# Readable alias for tests/imports that want to be explicit about the worker app.
WorkerSettings = Settings
