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


@pytest.mark.parametrize(
    "field_name",
    [
        "planning_lead_time_minutes",
        "planning_max_attempts",
        "planning_compaction_distance_m",
        "planning_max_group_requests",
    ],
)
def test_settings_reject_nonpositive_planning_values(field_name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: 0})


@pytest.mark.parametrize(
    "field_name",
    ["auth_access_token_ttl_seconds", "auth_refresh_session_ttl_seconds"],
)
def test_settings_reject_nonpositive_authentication_lifetimes(field_name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: 0})


def test_signing_keys_are_secret_aware() -> None:
    settings = Settings(
        _env_file=None,
        auth_jwt_private_key_pem="private-key-material",
        auth_jwt_public_key_pem="public-key-material",
    )
    assert "private-key-material" not in repr(settings)
    assert "public-key-material" not in repr(settings)
