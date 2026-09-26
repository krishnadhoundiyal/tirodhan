import pytest
from pydantic import ValidationError

from tirodhan.core.config import Settings


def test_settings_accept_postgresql_asyncpg_url() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://user:password@localhost/test",
    )

    assert settings.environment == "test"
    assert "password" not in repr(settings)


def test_settings_reject_non_postgresql_database() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:")
