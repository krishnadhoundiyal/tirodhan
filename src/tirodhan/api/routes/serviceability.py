from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.api.dependencies import (
    get_address_protector,
    get_current_user_id,
    get_database_session,
)
from tirodhan.api.routes.addresses import LocationInput
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.customers.ports import (
    AddressProtectionNotConfiguredError,
    AddressProtector,
)
from tirodhan.modules.customers.service import GeoPoint, IdempotencyCommandInProgressError
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.modules.serviceability.service import (
    CreateServiceabilityContextCommand,
    InvalidServiceabilityInputError,
    ServiceabilityContextNotFoundError,
    create_serviceability_context,
    get_serviceability_context,
)

router = APIRouter(prefix="/v1/serviceability/contexts", tags=["serviceability"])


class ServiceabilityContextCreateRequest(BaseModel):
    source_address_id: UUID | None = None
    address: str | None = Field(default=None, min_length=1, max_length=2000)
    location: LocationInput | None = None

    @model_validator(mode="after")
    def exactly_one_address_source(self) -> ServiceabilityContextCreateRequest:
        if (self.source_address_id is None) == (self.address is None):
            raise ValueError("provide exactly one of source_address_id or address")
        if self.source_address_id is not None and self.location is not None:
            raise ValueError("location is taken from the saved address")
        return self


class ServiceabilityContextResponse(BaseModel):
    serviceability_context_id: UUID
    source_address_id: UUID | None
    source_address_version: int | None
    status: str
    cell_id: str | None
    failure_code: str | None
    expires_at: datetime
    resolved_at: datetime | None


def _response(context: ServiceabilityContext) -> ServiceabilityContextResponse:
    return ServiceabilityContextResponse(
        serviceability_context_id=context.serviceability_context_id,
        source_address_id=context.source_address_id,
        source_address_version=context.source_address_version,
        status=context.status,
        cell_id=context.cell_id,
        failure_code=context.failure_code,
        expires_at=context.expires_at,
        resolved_at=context.resolved_at,
    )


def _configured_expiries(request: Request) -> tuple[datetime, datetime]:
    settings = cast(Settings, request.app.state.settings)
    context_seconds = settings.serviceability_context_ttl_seconds
    idempotency_seconds = settings.command_idempotency_ttl_seconds
    if context_seconds is None or idempotency_seconds is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Serviceability and idempotency retention must be configured",
        )
    now = utc_now()
    return (
        now + timedelta(seconds=context_seconds),
        now + timedelta(seconds=idempotency_seconds),
    )


def _translate_error(error: Exception) -> HTTPException:
    if isinstance(error, ServiceabilityContextNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(
        error,
        (
            IdempotencyKeyConflictError,
            IdempotencyCommandInProgressError,
            InvalidServiceabilityInputError,
        ),
    ):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, AddressProtectionNotConfiguredError):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error))
    raise error


@router.post("", response_model=ServiceabilityContextResponse, status_code=status.HTTP_201_CREATED)
async def post_serviceability_context(
    body: ServiceabilityContextCreateRequest,
    request: Request,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    protector: Annotated[AddressProtector, Depends(get_address_protector)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> ServiceabilityContextResponse:
    context_expires_at, idempotency_expires_at = _configured_expiries(request)
    location = (
        GeoPoint(latitude=body.location.latitude, longitude=body.location.longitude)
        if body.location is not None
        else None
    )
    try:
        context = await create_serviceability_context(
            session,
            CreateServiceabilityContextCommand(
                user_id=user_id,
                idempotency_key=idempotency_key,
                expires_at=context_expires_at,
                source_address_id=body.source_address_id,
                one_off_address=body.address,
                location=location,
            ),
            protector,
            idempotency_expires_at=idempotency_expires_at,
        )
        return _response(context)
    except Exception as error:
        raise _translate_error(error) from error


@router.get("/{context_id}", response_model=ServiceabilityContextResponse)
async def get_serviceability_context_status(
    context_id: UUID,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
) -> ServiceabilityContextResponse:
    """Read persisted status only; this route never invokes resolution."""
    try:
        return _response(
            await get_serviceability_context(
                session,
                context_id=context_id,
                user_id=user_id,
            )
        )
    except Exception as error:
        raise _translate_error(error) from error
