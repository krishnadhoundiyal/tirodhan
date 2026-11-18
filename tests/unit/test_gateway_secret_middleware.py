from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from starlette.middleware.cors import CORSMiddleware

from tirodhan.core.config import Settings
from tirodhan.main import GatewaySecretMiddleware


@pytest.mark.asyncio
async def test_gateway_secret_middleware_blocks_unauthorized_direct_access() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://user:password@localhost/test",
        gateway_secret=SecretStr("super-secret-gateway-token-12345"),
    )
    app = FastAPI()
    app.add_middleware(GatewaySecretMiddleware, settings=settings)

    @app.get("/v1/manager/collections")
    async def manager_endpoint():
        return {"data": "manager_data"}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        return {"status": "ready"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Direct hit without header -> 403 Forbidden
        response = await client.get("/v1/manager/collections")
        assert response.status_code == 403
        assert response.json() == {"error": {"code": "DIRECT_ACCESS_FORBIDDEN"}}
        assert response.headers["cache-control"] == "private, no-store"

        # 2. Direct hit with invalid header -> 403 Forbidden
        response_wrong = await client.get(
            "/v1/manager/collections", headers={"X-Gateway-Secret": "invalid-secret"}
        )
        assert response_wrong.status_code == 403
        assert response_wrong.json() == {"error": {"code": "DIRECT_ACCESS_FORBIDDEN"}}

        # 3. Mediated request through APIM with valid header -> 200 OK
        response_ok = await client.get(
            "/v1/manager/collections",
            headers={"X-Gateway-Secret": "super-secret-gateway-token-12345"},
        )
        assert response_ok.status_code == 200
        assert response_ok.json() == {"data": "manager_data"}

        # 4. Probes (/health and /ready) and provider webhook are exempt without gateway header
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 200

    @app.post("/v1/payments/provider/webhook")
    async def webhook():
        return {"received": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/v1/payments/provider/webhook")).status_code == 200

        # 5. Non-ASCII header value returns 403 without raising an unhandled exception
        response_non_ascii = await client.get(
            "/v1/manager/collections", headers=[(b"X-Gateway-Secret", b"invalid-\xff-token")]
        )
        assert response_non_ascii.status_code == 403
        assert response_non_ascii.json() == {"error": {"code": "DIRECT_ACCESS_FORBIDDEN"}}


@pytest.mark.asyncio
async def test_gateway_secret_middleware_allows_when_unconfigured() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://user:password@localhost/test",
        gateway_secret=None,
    )
    app = FastAPI()
    app.add_middleware(GatewaySecretMiddleware, settings=settings)

    @app.get("/v1/test")
    async def test_endpoint():
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # When gateway_secret is None (local unit tests), requests pass freely
        resp = await client.get("/v1/test")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


def test_create_app_rejects_wildcard_cors_origin() -> None:
    from tirodhan.main import create_app

    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://user:password@localhost/test",
        cors_allowed_origins=["*"],
    )
    with pytest.raises(ValueError, match=r"CORS wildcard '\*' is forbidden"):
        create_app(settings)


@pytest.mark.asyncio
async def test_cors_wraps_gateway_rejection_for_allowed_origin() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://user:password@localhost/test",
        gateway_secret=SecretStr("guard-secret-abc"),
        cors_allowed_origins=["https://customer.qtkservice.com"],
    )
    app = FastAPI()
    app.add_middleware(GatewaySecretMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/v1/customer/me")
    async def customer_endpoint():
        return {"data": "customer_data"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Request with allowed Origin but missing X-Gateway-Secret receives 403 wrapped by CORS
        response = await client.get(
            "/v1/customer/me",
            headers={"Origin": "https://customer.qtkservice.com"},
        )
        assert response.status_code == 403
        assert response.json() == {"error": {"code": "DIRECT_ACCESS_FORBIDDEN"}}
        assert response.headers["access-control-allow-origin"] == "https://customer.qtkservice.com"
        assert response.headers["access-control-allow-credentials"] == "true"
        assert response.headers["cache-control"] == "private, no-store"
