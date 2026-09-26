import pytest
from fastapi import FastAPI, HTTPException, status
from httpx import ASGITransport, AsyncClient

from tirodhan.api.routes.health import require_database_ready
from tirodhan.core.config import Settings
from tirodhan.main import create_app


@pytest.fixture
def app_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://user:password@localhost/test",
    )


@pytest.fixture
def application(app_settings: Settings) -> FastAPI:
    application = create_app(app_settings)

    async def database_is_ready() -> None:
        return None

    application.dependency_overrides[require_database_ready] = database_is_ready
    return application


@pytest.mark.asyncio
async def test_health_is_live_without_database_probe(application: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_reports_ready_when_database_probe_passes(
    application: FastAPI,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_readiness_reports_unavailable_when_database_probe_fails(
    app_settings: Settings,
) -> None:
    application = create_app(app_settings)

    async def database_is_not_ready() -> None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not ready",
        )

    application.dependency_overrides[require_database_ready] = database_is_not_ready
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json() == {"detail": "Database is not ready"}
