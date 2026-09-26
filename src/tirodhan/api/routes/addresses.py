from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from geoalchemy2.shape import to_shape
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.api.dependencies import (
    get_address_protector,
    get_current_user_id,
    get_database_session,
)
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.customers.models import UserAddress
from tirodhan.modules.customers.ports import (
    AddressProtectionNotConfiguredError,
    AddressProtector,
)
from tirodhan.modules.customers.service import (
    AddressNotFoundError,
    AddressVersionConflictError,
    CreateAddressCommand,
    GeoPoint,
    IdempotencyCommandInProgressError,
    UpdateAddressCommand,
    archive_address,
    create_address,
    list_active_addresses,
    update_address,
)
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

router = APIRouter(prefix="/v1/addresses", tags=["addresses"])


class LocationInput(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class AddressWriteRequest(BaseModel):
    address: str = Field(min_length=1, max_length=2000)
    label: str | None = Field(default=None, max_length=80)
    location: LocationInput | None = None
    is_default: bool = False


class AddressUpdateRequest(AddressWriteRequest):
    expected_version: int = Field(ge=1)


class AddressResponse(BaseModel):
    address_id: UUID
    label: str | None
    address: str
    location: LocationInput | None
    status: str
    is_default: bool
    version: int


def _point(value: LocationInput | None) -> GeoPoint | None:
    if value is None:
        return None
    return GeoPoint(latitude=value.latitude, longitude=value.longitude)


def _configured_expiry(request: Request) -> datetime:
    settings = cast(Settings, request.app.state.settings)
    seconds = settings.command_idempotency_ttl_seconds
    if seconds is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Command idempotency retention is not configured",
        )
    return utc_now() + timedelta(seconds=seconds)


async def _response(address: UserAddress, protector: AddressProtector) -> AddressResponse:
    location: LocationInput | None = None
    if address.location is not None:
        shape = cast(Any, to_shape(address.location))
        location = LocationInput(latitude=shape.y, longitude=shape.x)
    try:
        plaintext = await protector.unprotect(address.address_encrypted)
    except AddressProtectionNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error
    return AddressResponse(
        address_id=address.address_id,
        label=address.label,
        address=plaintext,
        location=location,
        status=address.status,
        is_default=address.is_default,
        version=address.version,
    )


def _translate_command_error(error: Exception) -> HTTPException:
    if isinstance(error, (IdempotencyKeyConflictError, AddressVersionConflictError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, IdempotencyCommandInProgressError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, AddressNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, AddressProtectionNotConfiguredError):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error))
    if isinstance(error, IntegrityError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Address conflicts with an existing active default",
        )
    raise error


@router.post("", response_model=AddressResponse, status_code=status.HTTP_201_CREATED)
async def post_address(
    body: AddressWriteRequest,
    request: Request,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    protector: Annotated[AddressProtector, Depends(get_address_protector)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> AddressResponse:
    try:
        address = await create_address(
            session,
            CreateAddressCommand(
                user_id=user_id,
                idempotency_key=idempotency_key,
                address=body.address,
                label=body.label,
                location=_point(body.location),
                is_default=body.is_default,
            ),
            protector,
            idempotency_expires_at=_configured_expiry(request),
        )
        return await _response(address, protector)
    except Exception as error:
        raise _translate_command_error(error) from error


@router.get("", response_model=list[AddressResponse])
async def get_addresses(
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    protector: Annotated[AddressProtector, Depends(get_address_protector)],
) -> list[AddressResponse]:
    return [
        await _response(address, protector)
        for address in await list_active_addresses(session, user_id)
    ]


@router.put("/{address_id}", response_model=AddressResponse)
async def put_address(
    address_id: UUID,
    body: AddressUpdateRequest,
    request: Request,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    protector: Annotated[AddressProtector, Depends(get_address_protector)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> AddressResponse:
    try:
        address = await update_address(
            session,
            UpdateAddressCommand(
                user_id=user_id,
                address_id=address_id,
                idempotency_key=idempotency_key,
                expected_version=body.expected_version,
                address=body.address,
                label=body.label,
                location=_point(body.location),
                is_default=body.is_default,
            ),
            protector,
            idempotency_expires_at=_configured_expiry(request),
        )
        return await _response(address, protector)
    except Exception as error:
        raise _translate_command_error(error) from error


@router.post("/{address_id}/archive", response_model=AddressResponse)
async def post_archive_address(
    address_id: UUID,
    request: Request,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    protector: Annotated[AddressProtector, Depends(get_address_protector)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> AddressResponse:
    try:
        address = await archive_address(
            session,
            user_id=user_id,
            address_id=address_id,
            idempotency_key=idempotency_key,
            idempotency_expires_at=_configured_expiry(request),
        )
        return await _response(address, protector)
    except Exception as error:
        raise _translate_command_error(error) from error
