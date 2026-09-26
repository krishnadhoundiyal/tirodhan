from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from geoalchemy2.elements import WKTElement
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.customers.models import UserAddress
from tirodhan.modules.customers.ports import AddressProtector
from tirodhan.modules.reliability.primitives import (
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)

ADDRESS_ACTIVE = "ACTIVE"
ADDRESS_ARCHIVED = "ARCHIVED"


class AddressNotFoundError(LookupError):
    pass


class AddressVersionConflictError(RuntimeError):
    pass


class IdempotencyCommandInProgressError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GeoPoint:
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class CreateAddressCommand:
    user_id: UUID
    idempotency_key: str
    address: str
    label: str | None = None
    location: GeoPoint | None = None
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class UpdateAddressCommand:
    user_id: UUID
    address_id: UUID
    idempotency_key: str
    expected_version: int
    address: str
    label: str | None = None
    location: GeoPoint | None = None
    is_default: bool = False


def command_fingerprint(data: dict[str, object]) -> bytes:
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).digest()


def geography_point(point: GeoPoint | None) -> WKTElement | None:
    if point is None:
        return None
    return WKTElement(f"POINT({point.longitude} {point.latitude})", srid=4326)


async def _replayed_address(
    session: AsyncSession, *, resource_id: UUID | None, user_id: UUID
) -> UserAddress:
    if resource_id is None:
        raise IdempotencyCommandInProgressError("the command has not completed")
    address = await session.scalar(
        select(UserAddress).where(
            UserAddress.address_id == resource_id,
            UserAddress.user_id == user_id,
        )
    )
    if address is None:
        raise RuntimeError("completed address command references a missing resource")
    return address


async def create_address(
    session: AsyncSession,
    command: CreateAddressCommand,
    protector: AddressProtector,
    *,
    idempotency_expires_at: datetime,
) -> UserAddress:
    fingerprint = command_fingerprint(
        {
            "user_id": command.user_id,
            "address": command.address,
            "label": command.label,
            "location": command.location,
            "is_default": command.is_default,
        }
    )
    claim = await claim_idempotency_record(
        session,
        scope=f"address.create:{command.user_id}",
        idempotency_key=command.idempotency_key,
        request_fingerprint=fingerprint,
        expires_at=idempotency_expires_at,
    )
    if not claim.created:
        result = get_completed_idempotency_result(claim.record)
        if result is None:
            raise IdempotencyCommandInProgressError("address creation is already in progress")
        return await _replayed_address(
            session, resource_id=result.resource_id, user_id=command.user_id
        )

    now = utc_now()
    address = UserAddress(
        address_id=new_uuid7(),
        user_id=command.user_id,
        label=command.label,
        address_encrypted=await protector.protect(command.address),
        location=geography_point(command.location),
        status=ADDRESS_ACTIVE,
        is_default=command.is_default,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(address)
    await session.flush([address])
    await complete_idempotency_record(
        session,
        claim.record,
        result_resource_id=address.address_id,
        result_status_code=201,
    )
    return address


async def list_active_addresses(session: AsyncSession, user_id: UUID) -> list[UserAddress]:
    addresses = await session.scalars(
        select(UserAddress)
        .where(UserAddress.user_id == user_id, UserAddress.status == ADDRESS_ACTIVE)
        .order_by(UserAddress.created_at, UserAddress.address_id)
    )
    return list(addresses)


async def update_address(
    session: AsyncSession,
    command: UpdateAddressCommand,
    protector: AddressProtector,
    *,
    idempotency_expires_at: datetime,
) -> UserAddress:
    fingerprint = command_fingerprint(
        {
            "user_id": command.user_id,
            "address_id": command.address_id,
            "expected_version": command.expected_version,
            "address": command.address,
            "label": command.label,
            "location": command.location,
            "is_default": command.is_default,
        }
    )
    claim = await claim_idempotency_record(
        session,
        scope=f"address.update:{command.address_id}",
        idempotency_key=command.idempotency_key,
        request_fingerprint=fingerprint,
        expires_at=idempotency_expires_at,
    )
    if not claim.created:
        result = get_completed_idempotency_result(claim.record)
        if result is None:
            raise IdempotencyCommandInProgressError("address update is already in progress")
        return await _replayed_address(
            session, resource_id=result.resource_id, user_id=command.user_id
        )

    address = await session.scalar(
        update(UserAddress)
        .where(
            UserAddress.address_id == command.address_id,
            UserAddress.user_id == command.user_id,
            UserAddress.status == ADDRESS_ACTIVE,
            UserAddress.version == command.expected_version,
        )
        .values(
            label=command.label,
            address_encrypted=await protector.protect(command.address),
            location=geography_point(command.location),
            is_default=command.is_default,
            version=UserAddress.version + 1,
            updated_at=utc_now(),
        )
        .returning(UserAddress)
    )
    if address is None:
        existing = await session.scalar(
            select(UserAddress).where(
                UserAddress.address_id == command.address_id,
                UserAddress.user_id == command.user_id,
            )
        )
        if existing is None or existing.status != ADDRESS_ACTIVE:
            raise AddressNotFoundError("active address not found")
        raise AddressVersionConflictError("address version is stale")

    await complete_idempotency_record(
        session,
        claim.record,
        result_resource_id=address.address_id,
        result_status_code=200,
    )
    return address


async def archive_address(
    session: AsyncSession,
    *,
    user_id: UUID,
    address_id: UUID,
    idempotency_key: str,
    idempotency_expires_at: datetime,
) -> UserAddress:
    fingerprint = command_fingerprint({"user_id": user_id, "address_id": address_id})
    claim = await claim_idempotency_record(
        session,
        scope=f"address.archive:{address_id}",
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        expires_at=idempotency_expires_at,
    )
    if not claim.created:
        result = get_completed_idempotency_result(claim.record)
        if result is None:
            raise IdempotencyCommandInProgressError("address archival is already in progress")
        return await _replayed_address(session, resource_id=result.resource_id, user_id=user_id)

    address = await session.scalar(
        update(UserAddress)
        .where(
            UserAddress.address_id == address_id,
            UserAddress.user_id == user_id,
            UserAddress.status == ADDRESS_ACTIVE,
        )
        .values(
            status=ADDRESS_ARCHIVED,
            is_default=False,
            version=UserAddress.version + 1,
            updated_at=utc_now(),
        )
        .returning(UserAddress)
    )
    if address is None:
        address = await session.scalar(
            select(UserAddress).where(
                UserAddress.address_id == address_id,
                UserAddress.user_id == user_id,
            )
        )
        if address is None:
            raise AddressNotFoundError("address not found")
        if address.status != ADDRESS_ARCHIVED:
            raise AddressVersionConflictError("address cannot be archived from its current state")

    await complete_idempotency_record(
        session,
        claim.record,
        result_resource_id=address.address_id,
        result_status_code=200,
    )
    return address
