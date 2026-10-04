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


@pytest.mark.parametrize("duration", [0, -1])
def test_settings_reject_nonpositive_rider_notification_lock_renewal(duration: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rider_notification_lock_renewal_seconds=duration)


def test_rider_notification_lock_renewal_is_optional_and_independent(monkeypatch) -> None:
    monkeypatch.delenv("TIRODHAN_RIDER_NOTIFICATION_LOCK_RENEWAL_SECONDS", raising=False)
    assert Settings(_env_file=None).rider_notification_lock_renewal_seconds is None
    monkeypatch.setenv("TIRODHAN_RIDER_NOTIFICATION_LOCK_RENEWAL_SECONDS", "180")
    monkeypatch.setenv("TIRODHAN_SERVICEABILITY_LOCK_RENEWAL_SECONDS", "90")
    settings = Settings(_env_file=None)
    assert settings.rider_notification_lock_renewal_seconds == 180
    assert settings.serviceability_lock_renewal_seconds == 90


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


def test_blob_account_url_must_be_https() -> None:
    import pytest

    from tirodhan.core.config import Settings

    with pytest.raises(ValueError, match="must use HTTPS"):
        Settings(media_blob_account_url="http://insecure.blob.core.windows.net")


def test_db_pool_limits_rejection() -> None:
    import pytest
    from pydantic import ValidationError

    from tirodhan.core.config import Settings

    # -1 is unlimited in sqlalchemy, but we explicitly disallow it.
    with pytest.raises(ValidationError, match="db_max_overflow must be non-negative"):
        Settings(db_max_overflow=-1)
