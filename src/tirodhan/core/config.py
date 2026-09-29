from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Tirodhan API"
    environment: Literal["local", "nonprod", "prod", "test"] = "local"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_file_path: Path | None = None
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://tirodhan:tirodhan@localhost:5432/tirodhan"
    )
    database_echo: bool = False
    command_idempotency_ttl_seconds: int | None = None
    serviceability_context_ttl_seconds: int | None = None
    pending_payment_lifetime_seconds: int | None = None
    auth_access_token_ttl_seconds: int | None = None
    auth_refresh_session_ttl_seconds: int | None = None
    auth_token_issuer: str | None = None
    auth_token_audience: str | None = None
    auth_jwt_private_key_pem: SecretStr | None = None
    auth_jwt_public_key_pem: SecretStr | None = None
    planning_lead_time_minutes: int | None = None
    planning_max_attempts: int | None = None
    planning_compaction_distance_m: int | None = None
    planning_max_group_requests: int | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TIRODHAN_",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("database_url")
    @classmethod
    def database_must_use_postgresql_asyncpg(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg driver")
        return value

    @field_validator(
        "command_idempotency_ttl_seconds",
        "serviceability_context_ttl_seconds",
        "pending_payment_lifetime_seconds",
        "auth_access_token_ttl_seconds",
        "auth_refresh_session_ttl_seconds",
        "planning_lead_time_minutes",
        "planning_max_attempts",
        "planning_compaction_distance_m",
        "planning_max_group_requests",
    )
    @classmethod
    def optional_ttl_must_be_positive(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("configured durations and attempt limits must be positive")
        return value

    @field_validator("auth_token_issuer", "auth_token_audience")
    @classmethod
    def optional_auth_identifier_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("authentication issuer and audience must not be blank")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
