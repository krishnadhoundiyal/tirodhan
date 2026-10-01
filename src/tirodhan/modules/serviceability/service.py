from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from geoalchemy2.shape import to_shape
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.customers.models import UserAddress
from tirodhan.modules.customers.ports import AddressProtector
from tirodhan.modules.customers.service import (
    ADDRESS_ACTIVE,
    GeoPoint,
    IdempotencyCommandInProgressError,
    command_fingerprint,
    geography_point,
)
from tirodhan.modules.reliability.primitives import (
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.modules.serviceability.ports import (
    CellIdDeriver,
    LocationResolutionStatus,
    LocationResolver,
)

SERVICEABILITY_PENDING = "PENDING"
SERVICEABILITY_SERVICEABLE = "SERVICEABLE"
SERVICEABILITY_UNSERVICEABLE = "UNSERVICEABLE"
SERVICEABILITY_TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
TERMINAL_SERVICEABILITY_STATUSES = frozenset(
    {
        SERVICEABILITY_SERVICEABLE,
        SERVICEABILITY_UNSERVICEABLE,
        SERVICEABILITY_TECHNICAL_FAILURE,
    }
)


class ServiceabilityContextNotFoundError(LookupError):
    pass


class InvalidServiceabilityInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CreateServiceabilityContextCommand:
    user_id: UUID
    idempotency_key: str
    expires_at: datetime
    source_address_id: UUID | None = None
    one_off_address: str | None = None
    location: GeoPoint | None = None


async def _load_owned_context(
    session: AsyncSession, *, context_id: UUID, user_id: UUID
) -> ServiceabilityContext:
    context = await session.scalar(
        select(ServiceabilityContext).where(
            ServiceabilityContext.serviceability_context_id == context_id,
            ServiceabilityContext.user_id == user_id,
        )
    )
    if context is None:
        raise ServiceabilityContextNotFoundError("serviceability context not found")
    return context


async def create_serviceability_context(
    session: AsyncSession,
    command: CreateServiceabilityContextCommand,
    protector: AddressProtector,
    *,
    idempotency_expires_at: datetime,
) -> ServiceabilityContext:
    if (command.source_address_id is None) == (command.one_off_address is None):
        raise InvalidServiceabilityInputError(
            "provide exactly one of source_address_id or one_off_address"
        )

    fingerprint = command_fingerprint(
        {
            "user_id": command.user_id,
            "source_address_id": command.source_address_id,
            "one_off_address": command.one_off_address,
            "location": command.location,
        }
    )
    claim = await claim_idempotency_record(
        session,
        scope=f"serviceability-context.create:{command.user_id}",
        idempotency_key=command.idempotency_key,
        request_fingerprint=fingerprint,
        expires_at=idempotency_expires_at,
    )
    if not claim.created:
        result = get_completed_idempotency_result(claim.record)
        if result is None or result.resource_id is None:
            raise IdempotencyCommandInProgressError(
                "serviceability context creation is already in progress"
            )
        return await _load_owned_context(
            session, context_id=result.resource_id, user_id=command.user_id
        )

    source_version: int | None = None
    if command.source_address_id is not None:
        source = await session.scalar(
            select(UserAddress).where(
                UserAddress.address_id == command.source_address_id,
                UserAddress.user_id == command.user_id,
                UserAddress.status == ADDRESS_ACTIVE,
            )
        )
        if source is None:
            raise ServiceabilityContextNotFoundError("active source address not found")
        protected_snapshot = bytes(source.address_encrypted)
        location = (
            geography_point(command.location) if command.location is not None else source.location
        )
        source_version = source.version
    else:
        if command.one_off_address is None:
            raise InvalidServiceabilityInputError("one-off address is required")
        protected_snapshot = await protector.protect(command.one_off_address)
        location = geography_point(command.location)

    now = utc_now()
    context = ServiceabilityContext(
        serviceability_context_id=new_uuid7(),
        user_id=command.user_id,
        source_address_id=command.source_address_id,
        source_address_version=source_version,
        address_snapshot_encrypted=protected_snapshot,
        location=location,
        status=SERVICEABILITY_PENDING,
        expires_at=command.expires_at,
        created_at=now,
    )
    session.add(context)
    await session.flush([context])
    await append_outbox_event(
        session,
        event_key=f"serviceability-requested:{context.serviceability_context_id}",
        aggregate_type="serviceability_context",
        aggregate_id=context.serviceability_context_id,
        event_type="ServiceabilityRequested",
        payload={"serviceability_context_id": str(context.serviceability_context_id)},
    )
    await complete_idempotency_record(
        session,
        claim.record,
        result_resource_id=context.serviceability_context_id,
        result_status_code=201,
    )
    return context


async def get_serviceability_context(
    session: AsyncSession, *, context_id: UUID, user_id: UUID
) -> ServiceabilityContext:
    return await _load_owned_context(session, context_id=context_id, user_id=user_id)


def _stored_location(context: ServiceabilityContext) -> GeoPoint | None:
    if context.location is None:
        return None
    point = cast(Any, to_shape(context.location))
    return GeoPoint(latitude=point.y, longitude=point.x)


async def resolve_serviceability(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    context_id: UUID,
    protector: AddressProtector,
    location_resolver: LocationResolver,
    cell_id_deriver: CellIdDeriver,
) -> ServiceabilityContext:
    """Resolve once; PostgreSQL conditionally selects the authoritative result."""
    async with session_factory() as session, session.begin():
        context = await session.get(ServiceabilityContext, context_id)
        if context is None:
            raise ServiceabilityContextNotFoundError("serviceability context not found")
        if context.status in TERMINAL_SERVICEABILITY_STATUSES:
            return context
        snapshot = bytes(context.address_snapshot_encrypted)
        supplied_location = _stored_location(context)

    address = await protector.unprotect(snapshot)
    outcome = await location_resolver.resolve(
        address=address,
        supplied_location=supplied_location,
    )

    cell_id: str | None = None
    failure_code = outcome.failure_code
    if outcome.status == LocationResolutionStatus.RESOLVED:
        if outcome.location is None:
            raise InvalidServiceabilityInputError("resolved outcome requires a location")
        terminal_status = SERVICEABILITY_SERVICEABLE
        cell_id = await cell_id_deriver.derive(outcome.location)
        if not cell_id:
            raise InvalidServiceabilityInputError("cell derivation returned an empty identifier")
    elif outcome.status == LocationResolutionStatus.UNSERVICEABLE:
        terminal_status = SERVICEABILITY_UNSERVICEABLE
    elif outcome.status == LocationResolutionStatus.TECHNICAL_FAILURE:
        terminal_status = SERVICEABILITY_TECHNICAL_FAILURE
        if not failure_code:
            raise InvalidServiceabilityInputError(
                "technical failure outcome requires a failure code"
            )
    else:
        raise InvalidServiceabilityInputError("unknown location resolution outcome")

    values: dict[str, object] = {
        "status": terminal_status,
        "cell_id": cell_id,
        "failure_code": failure_code,
        "resolved_at": utc_now(),
    }
    if outcome.location is not None:
        values["location"] = geography_point(outcome.location)

    async with session_factory() as session, session.begin():
        authoritative = await session.scalar(
            update(ServiceabilityContext)
            .where(
                ServiceabilityContext.serviceability_context_id == context_id,
                ServiceabilityContext.status == SERVICEABILITY_PENDING,
            )
            .values(**values)
            .returning(ServiceabilityContext)
        )
        if authoritative is not None:
            return authoritative

        established = await session.get(ServiceabilityContext, context_id)
        if established is None or established.status not in TERMINAL_SERVICEABILITY_STATUSES:
            raise RuntimeError("serviceability resolution lost without a terminal result")
        return established
