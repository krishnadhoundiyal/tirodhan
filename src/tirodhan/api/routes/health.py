from __future__ import annotations

import logging
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from tirodhan.db.session import is_database_ready

logger = logging.getLogger(__name__)
router = APIRouter(tags=["platform"])


class HealthResponse(BaseModel):
    status: str


def get_database_engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.database_engine)


async def require_database_ready(
    engine: Annotated[AsyncEngine, Depends(get_database_engine)],
) -> None:
    try:
        if await is_database_ready(engine):
            return
    except Exception as error:
        logger.warning(
            "database_readiness_check_failed",
            extra={"error_type": type(error).__name__},
        )

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Database is not ready",
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe: the API process can serve requests."""
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=HealthResponse,
    dependencies=[Depends(require_database_ready)],
)
async def readiness() -> HealthResponse:
    """Readiness probe: PostgreSQL is reachable and PostGIS is enabled."""
    return HealthResponse(status="ready")
