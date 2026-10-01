from __future__ import annotations

import math
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
    google_maps_api_key: SecretStr | None = None
    google_maps_http_timeout_seconds: float | None = None
    google_maps_delhi_admin_aliases: list[str] = [
        "Delhi",
        "DL",
        "National Capital Territory of Delhi",
        "NCT of Delhi",
    ]
    service_bus_namespace: str | None = None
    serviceability_queue_name: str | None = None
    service_bus_managed_identity_client_id: str | None = None
    service_bus_operation_timeout_seconds: float | None = None
    serviceability_lock_renewal_seconds: int | None = None
    outbox_publish_batch_size: int | None = None
    command_idempotency_ttl_seconds: int | None = None
    serviceability_context_ttl_seconds: int | None = None
    pending_payment_lifetime_seconds: int | None = None
    auth_access_token_ttl_seconds: int | None = None
    auth_refresh_session_ttl_seconds: int | None = None
    auth_otp_challenge_ttl_seconds: int | None = None
    kaleyra_api_domain: str | None = None
    kaleyra_sid: str | None = None
    kaleyra_api_key: SecretStr | None = None
    kaleyra_verify_flow_id: str | None = None
    kaleyra_http_timeout_seconds: float | None = None
    phone_encryption_active_key_id: str | None = None
    phone_encryption_keys: SecretStr | None = None
    phone_lookup_hmac_key: SecretStr | None = None
    address_encryption_active_key_id: str | None = None
    address_encryption_keys: SecretStr | None = None
    auth_token_issuer: str | None = None
    auth_token_audience: str | None = None
    auth_jwt_private_key_pem: SecretStr | None = None
    auth_jwt_public_key_pem: SecretStr | None = None
    planning_lead_time_minutes: int | None = None
    planning_max_attempts: int | None = None
    planning_compaction_distance_m: int | None = None
    planning_max_group_requests: int | None = None

    media_blob_account_url: str | None = None
    media_blob_container_name: str | None = None
    media_upload_authorization_ttl_seconds: int | None = None

    media_photo_allowed_content_types: list[str] | None = None
    media_photo_max_size_bytes: int | None = None
    media_video_allowed_content_types: list[str] | None = None
    media_video_max_size_bytes: int | None = None

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
        "auth_otp_challenge_ttl_seconds",
        "planning_lead_time_minutes",
        "planning_max_attempts",
        "planning_compaction_distance_m",
        "planning_max_group_requests",
        "media_upload_authorization_ttl_seconds",
        "media_photo_max_size_bytes",
        "media_video_max_size_bytes",
        "serviceability_lock_renewal_seconds",
        "outbox_publish_batch_size",
    )
    @classmethod
    def optional_ttl_must_be_positive(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("configured durations and attempt limits must be positive")
        return value

    @field_validator(
        "auth_token_issuer",
        "auth_token_audience",
        "media_blob_account_url",
        "media_blob_container_name",
    )
    @classmethod
    def optional_auth_identifier_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("authentication issuer and audience must not be blank")
        return value

    @field_validator("media_blob_account_url")
    @classmethod
    def optional_blob_account_url_must_be_https(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("Azure Blob account URL must use HTTPS")
        return value

    @field_validator("kaleyra_api_domain")
    @classmethod
    def kaleyra_domain_must_be_https(cls, value: str | None) -> str | None:
        if value is not None:
            from tirodhan.modules.identity.kaleyra import validate_api_domain

            value = validate_api_domain(value)
        return value

    @field_validator("kaleyra_http_timeout_seconds")
    @classmethod
    def provider_timeout_must_be_bounded(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("provider timeout must be finite and positive")
        return value

    @field_validator("google_maps_http_timeout_seconds", "service_bus_operation_timeout_seconds")
    @classmethod
    def runtime_timeout_must_be_bounded(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or not 0 < value <= 60):
            raise ValueError("runtime timeout must be finite and within (0, 60] seconds")
        return value

    @field_validator("google_maps_delhi_admin_aliases")
    @classmethod
    def aliases_must_be_nonempty(cls, value: list[str]) -> list[str]:
        if not value or any(not alias.strip() for alias in value):
            raise ValueError("Delhi administrative aliases must not be empty")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
