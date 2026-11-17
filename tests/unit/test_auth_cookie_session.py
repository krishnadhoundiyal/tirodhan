from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import SecretStr

from tirodhan.api.routes.auth import (
    AccessTokenResponse,
    LogoutRequest,
    RefreshRequest,
    _cookie_name,
    post_logout,
    post_refresh,
)
from tirodhan.core.config import Settings
from tirodhan.modules.identity.service import LoginCredentials


def make_test_settings(*, secure: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://user:password@localhost/test",
        auth_cookie_secure=secure,
        auth_cookie_name="__Host-tirodhan_refresh",
        auth_cookie_samesite="lax",
        auth_access_token_ttl_seconds=900,
        auth_refresh_session_ttl_seconds=86400,
        command_idempotency_ttl_seconds=300,
    )


def test_cookie_name_standardization() -> None:
    # RFC 6265bis: __Host- prefix requires secure=True.
    secure_settings = make_test_settings(secure=True)
    assert _cookie_name(secure_settings) == "__Host-tirodhan_refresh"

    # When insecure (e.g. plain HTTP local dev), __Host- is stripped so browsers accept it.
    insecure_settings = make_test_settings(secure=False)
    assert _cookie_name(insecure_settings) == "tirodhan_refresh"


def test_auth_request_schemas_support_both_body_and_cookie() -> None:
    # Mobile flow: refresh_token provided in body
    assert RefreshRequest(refresh_token=SecretStr("token_abc")).refresh_token is not None
    assert LogoutRequest(refresh_token=SecretStr("token_abc")).refresh_token is not None

    # Browser flow: empty body (relies on cookie)
    assert RefreshRequest().refresh_token is None
    assert LogoutRequest().refresh_token is None


@pytest.mark.asyncio
async def test_post_refresh_route_selects_cookie_credentials() -> None:
    settings = make_test_settings(secure=True)
    app = FastAPI()
    app.state.settings = settings
    scope = {
        "type": "http",
        "app": app,
        "headers": [(b"cookie", b"__Host-tirodhan_refresh=cookie_token_12345")],
    }
    request = Request(scope)

    expected_user_id = uuid4()
    mock_credentials = LoginCredentials(
        access_token=SecretStr("fresh_jwt_access_token"),
        refresh_token=SecretStr("cookie_token_12345"),
        expires_in=900,
        user_id=expected_user_id,
        refresh_session_id=uuid4(),
    )

    with patch(
        "tirodhan.api.routes.auth.refresh_access_token",
        new=AsyncMock(return_value=mock_credentials),
    ) as mock_refresh:
        # Browser client sends empty body (body=None)
        response = await post_refresh(
            request=request,
            session_factory=AsyncMock(),
            token_codec=AsyncMock(),
            body=None,
        )

        assert isinstance(response, AccessTokenResponse)
        assert response.access_token == "fresh_jwt_access_token"
        assert response.user_id == expected_user_id
        # Proves cookie value was selected and passed to service
        mock_refresh.assert_awaited_once()
        assert mock_refresh.await_args.kwargs["raw_refresh_credential"] == "cookie_token_12345"


@pytest.mark.asyncio
async def test_post_refresh_route_selects_body_credentials_for_mobile() -> None:
    settings = make_test_settings(secure=True)
    app = FastAPI()
    app.state.settings = settings
    scope = {"type": "http", "app": app, "headers": []}  # No cookie
    request = Request(scope)

    expected_user_id = uuid4()
    mock_credentials = LoginCredentials(
        access_token=SecretStr("fresh_jwt_mobile"),
        refresh_token=SecretStr("mobile_body_token"),
        expires_in=900,
        user_id=expected_user_id,
        refresh_session_id=uuid4(),
    )

    with patch(
        "tirodhan.api.routes.auth.refresh_access_token",
        new=AsyncMock(return_value=mock_credentials),
    ) as mock_refresh:
        # Mobile client sends refresh_token in JSON body
        body = RefreshRequest(refresh_token=SecretStr("mobile_body_token"))
        response = await post_refresh(
            request=request,
            session_factory=AsyncMock(),
            token_codec=AsyncMock(),
            body=body,
        )

        assert response.access_token == "fresh_jwt_mobile"
        mock_refresh.assert_awaited_once()
        assert mock_refresh.await_args.kwargs["raw_refresh_credential"] == "mobile_body_token"


@pytest.mark.asyncio
async def test_post_refresh_route_rejects_when_both_cookie_and_body_absent() -> None:
    settings = make_test_settings(secure=True)
    app = FastAPI()
    app.state.settings = settings
    scope = {"type": "http", "app": app, "headers": []}  # No cookie
    request = Request(scope)

    with pytest.raises(HTTPException) as exc_info:
        await post_refresh(
            request=request,
            session_factory=AsyncMock(),
            token_codec=AsyncMock(),
            body=RefreshRequest(),  # Empty body
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Authentication failed"


@pytest.mark.asyncio
async def test_post_logout_route_revokes_and_deletes_cookie_with_root_path() -> None:
    settings = make_test_settings(secure=True)
    app = FastAPI()
    app.state.settings = settings
    scope = {
        "type": "http",
        "app": app,
        "headers": [(b"cookie", b"__Host-tirodhan_refresh=cookie_token_to_logout")],
    }
    request = Request(scope)
    response = Response()

    with patch(
        "tirodhan.api.routes.auth.logout_refresh_session",
        new=AsyncMock(),
    ) as mock_logout:
        # Browser client calls logout with cookie
        res = await post_logout(
            request=request,
            response=response,
            session_factory=AsyncMock(),
            body=None,
        )

        assert res.status_code == 204
        # Proves database session revocation was invoked
        mock_logout.assert_awaited_once()
        assert mock_logout.await_args.kwargs["raw_refresh_credential"] == "cookie_token_to_logout"

        # Proves cookie is deleted with Path=/ (RFC 6265bis compliance for __Host- cookies)
        set_cookie = res.headers.get("set-cookie", "")
        assert "__Host-tirodhan_refresh" in set_cookie
        assert "path=/" in set_cookie.lower()
        assert "max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower()
