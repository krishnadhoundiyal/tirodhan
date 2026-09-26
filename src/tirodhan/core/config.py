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


@lru_cache
def get_settings() -> Settings:
    return Settings()
