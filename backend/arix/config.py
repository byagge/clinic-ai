"""Application settings.

Everything that differs between deployments lives here and is read from the
environment.  Per-tenant behaviour never lives here — it belongs in
``tenant_config`` (see ``arix.core.tenant_config``).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    # ---- runtime -----------------------------------------------------------
    env: Literal["local", "demo", "staging", "prod"] = "local"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = False
    public_base_url: str = "http://localhost:8000"
    secret_key: str = "change-me-in-production-please-32b"

    # ---- storage -----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/arix"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    redis_url: str = "redis://localhost:6379/0"

    # ``data_region`` is informational: it is stamped onto tenants created by
    # this deployment so that a KZ instance can be audited as KZ-resident.
    data_region: str = "local"

    # ---- LLM ---------------------------------------------------------------
    anthropic_api_key: str | None = None
    llm_dialog_model: str = "claude-sonnet-5-5"
    llm_fast_model: str = "claude-haiku-5-5"
    llm_max_tokens: int = 900
    llm_timeout_s: float = 30.0
    llm_max_tool_rounds: int = 6
    # When no API key is configured the deterministic scripted brain is used so
    # that demos and CI run without external dependencies.
    llm_force_scripted: bool = False

    # ---- speech to text ----------------------------------------------------
    stt_provider: Literal["none", "openai", "local"] = "none"
    stt_api_key: str | None = None
    stt_base_url: str = "https://api.openai.com/v1"
    stt_model: str = "whisper-1"

    # ---- telegram ----------------------------------------------------------
    # Shared ARIX bot used for demos and for tenants without their own bot.
    telegram_shared_bot_token: str | None = None
    telegram_webhook_secret: str = "tg-webhook-secret"
    # Internal ops bot (alerts about dead jobs, broken channels).
    telegram_ops_bot_token: str | None = None
    telegram_ops_chat_id: str | None = None

    # ---- whatsapp ----------------------------------------------------------
    # Baileys sidecar (no Meta Business API fees). See docs/WHATSAPP_BAILEYS.md
    wa_provider: Literal["baileys", "cloud_api", "none"] = "baileys"
    wa_baileys_url: str = "http://waweb:3000"
    wa_baileys_token: str = "baileys-shared-token"
    wa_webhook_secret: str = "wa-webhook-secret"
    wa_cloud_api_token: str | None = None
    wa_cloud_api_version: str = "v21.0"
    # Free-tier guard for Cloud API tenants only; Baileys has no per-message fee.
    wa_free_tier_monthly: int = 1000

    # ---- PII ---------------------------------------------------------------
    pii_masking_enabled: bool = True
    # Encryption key for channel credentials at rest (urlsafe base64, 32 bytes).
    credentials_encryption_key: str | None = None

    # ---- scheduler ---------------------------------------------------------
    job_poll_interval_s: float = 5.0
    job_batch_size: int = 50
    job_max_attempts: int = 5
    hold_ttl_minutes: int = 10
    handoff_pickup_minutes: int = 10
    handoff_escalate_minutes: int = 30

    # When true, no message leaves the process: deliveries are recorded in
    # ``DryRunAdapter.outbox`` and still stored in ``messages``.  Used by tests,
    # the eval suite and offline demos.
    channels_dry_run: bool = False

    # ---- demo --------------------------------------------------------------
    demo_ttl_days: int = 14
    demo_enabled: bool = True

    # ---- admin API ---------------------------------------------------------
    admin_api_token: str = "dev-admin-token"
    jwt_ttl_minutes: int = 720

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        # Accept plain postgres:// URLs (what hosting panels hand out) and make
        # them asyncpg-friendly.
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "+psycopg2").replace(
            "postgresql+psycopg2", "postgresql"
        )

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key) and not self.llm_force_scripted

    vertical: Literal["clinic", "edu"] = Field(
        default="clinic", description="Vertical module bundled in this deployment"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
