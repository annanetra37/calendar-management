"""Application settings.

Every secret is read from the environment. The app fails fast at import/boot
time with a clear message when a required variable is missing (T-03).
"""

from __future__ import annotations

import functools
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised when the process is not configured well enough to start."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Infrastructure -------------------------------------------------
    database_url: str = Field(..., description="Postgres DSN, injected by Railway")
    environment: Literal["development", "staging", "production", "test"] = "development"
    public_base_url: str = Field(
        ...,
        description="Stable HTTPS origin, e.g. https://scheduler.example.com (T-32)",
    )

    # --- Crypto ---------------------------------------------------------
    fernet_key: str = Field(..., description="urlsafe base64 32-byte key")

    # --- Google ---------------------------------------------------------
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    google_webhook_token: str = Field(
        default="",
        description="Opaque token echoed back by Google push notifications",
    )

    # --- Telegram -------------------------------------------------------
    telegram_bot_token: str
    telegram_webhook_secret: str
    telegram_allowed_user_ids: str = Field(
        default="",
        description="Comma-separated Telegram user ids permitted to use the bot",
    )

    # --- Models ---------------------------------------------------------
    openai_api_key: str
    openai_base_url: str = "https://api.openai.com/v1"
    whisper_model: str = "whisper-1"
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    extraction_provider: Literal["anthropic", "openai"] = "anthropic"
    extraction_model: str = "claude-sonnet-5"
    openai_extraction_model: str = "gpt-4o-2024-11-20"

    # --- Behaviour ------------------------------------------------------
    default_timezone: str = "Asia/Yerevan"
    default_duration_minutes: int = 60
    max_audio_seconds: int = 120
    max_audio_bytes: int = 20 * 1024 * 1024
    audio_retention_days: int = 30
    pending_card_ttl_minutes: int = 60
    commands_per_hour: int = 30
    slow_pipeline_notice_seconds: float = 15.0
    audio_storage_dir: str = "/tmp/voice-calendar-audio"

    # --- Observability --------------------------------------------------
    sentry_dsn: str = ""
    log_level: str = "INFO"
    log_transcripts: bool = False

    @field_validator("default_timezone")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - config error path
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("public_base_url", "google_redirect_uri")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def _normalise_dsn(cls, value: str) -> str:
        # Railway hands out postgres:// which SQLAlchemy 2 does not accept.
        if value.startswith("postgres://"):
            value = "postgresql+psycopg://" + value[len("postgres://") :]
        elif value.startswith("postgresql://"):
            value = "postgresql+psycopg://" + value[len("postgresql://") :]
        return value

    @property
    def allowed_telegram_ids(self) -> frozenset[int]:
        ids = set()
        for chunk in self.telegram_allowed_user_ids.split(","):
            chunk = chunk.strip()
            if chunk:
                ids.add(int(chunk))
        return frozenset(ids)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"])
            missing.append(f"  - {loc.upper()}: {err['msg']}")
        raise ConfigError(
            "Refusing to start: invalid or missing configuration.\n"
            + "\n".join(missing)
            + "\nSee .env.example for the full list of required variables."
        ) from exc
