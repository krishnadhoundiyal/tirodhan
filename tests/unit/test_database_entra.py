from unittest.mock import MagicMock, patch

import pytest

from tirodhan.core.config import Settings
from tirodhan.db.session import _async_creator, create_database_engine


@pytest.mark.asyncio
async def test_async_creator_parses_url_and_uses_entra_token() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://mockuser:mockpass@dbhost.internal:5432/mockdb",
        database_entra_authentication=True,
        database_managed_identity_client_id="client-id-123",
    )

    mock_token = MagicMock()
    mock_token.token = "fake-entra-token"

    mock_credential = MagicMock()
    mock_credential.get_token.return_value = mock_token

    mock_credential_cls = MagicMock(return_value=mock_credential)
    mock_credential_cls.__aenter__.return_value = mock_credential
    mock_credential_cls.__aexit__.return_value = None
    # async mocks need this sometimes in older libs
    mock_credential.get_token.__qualname__ = "get_token"

    async def async_get_token(*args, **kwargs):
        return mock_token
    mock_credential.get_token = async_get_token

    with patch("tirodhan.db.session.DefaultAzureCredential", return_value=mock_credential_cls):
        mock_credential_cls.return_value.__aenter__ = MagicMock(return_value=mock_credential)
        # Needs to be proper async mock for aenter
        async def mock_aenter(self):
            return mock_credential
        async def mock_aexit(self, exc_type, exc_val, exc_tb):
            pass

        mock_credential_cls.__aenter__ = mock_aenter
        mock_credential_cls.__aexit__ = mock_aexit

        with patch("asyncpg.connect") as mock_connect, patch(
            "ssl.create_default_context"
        ) as mock_ssl:
            mock_ssl.return_value = "fake-ssl-context"
            await _async_creator(settings)

            mock_connect.assert_called_once_with(
                host="dbhost.internal",
                port=5432,
                user="mockuser",
                password="fake-entra-token",
                database="mockdb",
                ssl="fake-ssl-context",
            )


def test_create_database_engine_applies_pool_settings() -> None:
    settings = Settings(
        _env_file=None,
        db_pool_size=15,
        db_max_overflow=5,
        db_pool_timeout=10.0,
    )

    with patch("tirodhan.db.session.create_async_engine") as mock_engine:
        create_database_engine(settings)
        args, kwargs = mock_engine.call_args
        assert kwargs["pool_size"] == 15
        assert kwargs["max_overflow"] == 5
        assert kwargs["pool_timeout"] == 10.0


def test_create_database_engine_applies_null_pool() -> None:
    settings = Settings(_env_file=None, db_pool_size=15)
    with patch("tirodhan.db.session.create_async_engine") as mock_engine:
        create_database_engine(settings, use_null_pool=True)
        args, kwargs = mock_engine.call_args
        assert "pool_size" not in kwargs
        from sqlalchemy.pool import NullPool
        assert kwargs["poolclass"] == NullPool
