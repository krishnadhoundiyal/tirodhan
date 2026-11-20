from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from tirodhan.api.dependencies import (
    get_access_token_codec,
    get_otp_provider,
    get_session_factory,
)
from tirodhan.api.routes.auth import (
    LoginTokenResponse,
    LogoutRequest,
    RefreshRequest,
    router,
)
from tirodhan.core.config import Settings
from tirodhan.main import create_app
from tirodhan.modules.identity.service import AccessCredentials, LoginCredentials


def test_refresh_request_validation() -> None:
    # 1. Valid non-empty token
    req = RefreshRequest.model_validate({"refresh_token": "valid.token.value"})
    assert req.refresh_token.get_secret_value() == "valid.token.value"

    # 2. Empty string is rejected
    with pytest.raises(ValidationError, match="refresh_token must not be empty or blank"):
        RefreshRequest.model_validate({"refresh_token": ""})

    # 3. Whitespace-only string is rejected
    with pytest.raises(ValidationError, match="refresh_token must not be empty or blank"):
        RefreshRequest.model_validate({"refresh_token": "   \t\n  "})

    # 4. Extra fields are forbidden
    with pytest.raises(ValidationError, match="extra"):
        RefreshRequest.model_validate({"refresh_token": "valid.token", "unknown_field": "injected"})


def test_logout_request_validation() -> None:
    # 1. Valid non-empty token
    req = LogoutRequest.model_validate({"refresh_token": "valid.token.value"})
    assert req.refresh_token.get_secret_value() == "valid.token.value"

    # 2. Empty string is rejected
    with pytest.raises(ValidationError, match="refresh_token must not be empty or blank"):
        LogoutRequest.model_validate({"refresh_token": ""})

    # 3. Whitespace-only string is rejected
    with pytest.raises(ValidationError, match="refresh_token must not be empty or blank"):
        LogoutRequest.model_validate({"refresh_token": "   "})

    # 4. Extra fields are forbidden
    with pytest.raises(ValidationError, match="extra"):
        LogoutRequest.model_validate({"refresh_token": "valid.token", "extra": "data"})


def test_login_token_response_contains_refresh_token_for_mobile_and_apim() -> None:
    user_id = uuid4()
    resp = LoginTokenResponse(
        access_token="test.jwt.access",
        refresh_token="test.opaque.refresh",
        token_type="bearer",
        expires_in=900,
        user_id=user_id,
    )
    dumped = resp.model_dump()
    assert dumped["access_token"] == "test.jwt.access"
    assert dumped["refresh_token"] == "test.opaque.refresh"
    assert dumped["token_type"] == "bearer"
    assert dumped["expires_in"] == 900
    assert dumped["user_id"] == user_id


@pytest.mark.asyncio
async def test_auth_routes_strictly_enforce_json_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://user:password@localhost/test",
        auth_access_token_ttl_seconds=900,
        auth_refresh_session_ttl_seconds=2592000,
        command_idempotency_ttl_seconds=86400,
    )

    app = FastAPI()
    app.state.settings = settings
    app.include_router(router)

    mock_session_factory = AsyncMock()
    mock_token_codec = AsyncMock()
    mock_token_codec.configured = True

    app.dependency_overrides[get_session_factory] = lambda: mock_session_factory
    app.dependency_overrides[get_access_token_codec] = lambda: mock_token_codec

    refreshed_user_id = uuid4()
    mock_refresh_service = AsyncMock(
        return_value=AccessCredentials(
            refresh_session_id=uuid4(),
            user_id=refreshed_user_id,
            access_token=SecretStr("new.access.token"),
            expires_in=900,
        )
    )
    mock_logout_service = AsyncMock()

    monkeypatch.setattr("tirodhan.api.routes.auth.refresh_access_token", mock_refresh_service)
    monkeypatch.setattr("tirodhan.api.routes.auth.logout_refresh_session", mock_logout_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Missing JSON body -> 422 Unprocessable Entity
        resp = await client.post("/v1/auth/refresh")
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        # 2. Empty refresh_token -> 422 Unprocessable Entity
        resp = await client.post("/v1/auth/refresh", json={"refresh_token": ""})
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        # 3. Whitespace refresh_token -> 422 Unprocessable Entity
        resp = await client.post("/v1/auth/refresh", json={"refresh_token": "   "})
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        # 4. Unknown extra field -> 422 Unprocessable Entity
        resp = await client.post(
            "/v1/auth/refresh",
            json={"refresh_token": "valid.token", "csrf_token": "unexpected"},
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        # 5. Valid refresh request -> 200 OK
        resp = await client.post("/v1/auth/refresh", json={"refresh_token": "valid.token"})
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["access_token"] == "new.access.token"
        assert "refresh_token" not in data  # Refresh endpoint only returns new access token

        # 6. Logout with empty token -> 422 Unprocessable Entity
        resp = await client.post("/v1/auth/logout", json={"refresh_token": "  "})
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        # 7. Valid logout request -> 204 No Content
        resp = await client.post("/v1/auth/logout", json={"refresh_token": "valid.token"})
        assert resp.status_code == status.HTTP_204_NO_CONTENT
        mock_logout_service.assert_awaited_once_with(
            mock_session_factory,
            raw_refresh_credential="valid.token",
        )


@pytest.mark.asyncio
async def test_gateway_forwarded_auth_remains_json_only(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "synthetic-gateway-token-" + "x" * 32
    app = create_app(
        Settings(
            _env_file=None,
            environment="nonprod",
            gateway_secret=secret,
            auth_access_token_ttl_seconds=900,
            auth_refresh_session_ttl_seconds=2592000,
            command_idempotency_ttl_seconds=86400,
        )
    )
    factory, codec, provider = AsyncMock(), AsyncMock(), AsyncMock()
    codec.configured = True
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_access_token_codec] = lambda: codec
    app.dependency_overrides[get_otp_provider] = lambda: provider
    credentials = LoginCredentials(
        user_id=uuid4(),
        refresh_session_id=uuid4(),
        access_token=SecretStr("synthetic.access"),
        refresh_token=SecretStr("synthetic.refresh"),
        expires_in=900,
    )
    login = AsyncMock(return_value=credentials)
    refresh = AsyncMock(
        return_value=AccessCredentials(
            user_id=credentials.user_id,
            refresh_session_id=credentials.refresh_session_id,
            access_token=credentials.access_token,
            expires_in=900,
        )
    )
    logout = AsyncMock()
    monkeypatch.setattr("tirodhan.api.routes.auth.verify_otp_and_login", login)
    monkeypatch.setattr("tirodhan.api.routes.auth.refresh_access_token", refresh)
    monkeypatch.setattr("tirodhan.api.routes.auth.logout_refresh_session", logout)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        headers = {"X-Gateway-Secret": secret, "Cookie": "__Host-customer_refresh=other-session"}
        response = await client.post(
            "/v1/auth/otp/verify",
            headers=headers,
            json={
                "client_login_id": str(uuid4()),
                "challenge_reference": str(uuid4()),
                "code": "123456",
            },
        )
        assert response.status_code == 200
        assert response.json()["refresh_token"] == "synthetic.refresh"
        assert "set-cookie" not in response.headers
        for path in ("/v1/auth/refresh", "/v1/auth/logout"):
            response = await client.post(path, headers=headers, json={})
            assert response.status_code == 422  # A browser cookie cannot replace the JSON contract.
            assert "set-cookie" not in response.headers
        refresh.assert_not_called()
        logout.assert_not_called()
        response = await client.post(
            "/v1/auth/refresh", headers=headers, json={"refresh_token": "synthetic.refresh"}
        )
        assert response.status_code == 200
        assert "refresh_token" not in response.json()
        assert "set-cookie" not in response.headers
        assert refresh.call_args.kwargs["raw_refresh_credential"] == "synthetic.refresh"
        for _ in range(2):
            response = await client.post(
                "/v1/auth/logout", headers=headers, json={"refresh_token": "synthetic.refresh"}
            )
            assert response.status_code == 204
            assert "set-cookie" not in response.headers
        assert logout.await_count == 2
        assert logout.call_args.kwargs["raw_refresh_credential"] == "synthetic.refresh"
