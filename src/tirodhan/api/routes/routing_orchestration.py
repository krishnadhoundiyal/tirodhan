"""Default-disabled Increment 7 contracts; native planning stays in finite workers."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_session_factory, require_role
from tirodhan.db.values import utc_now
from tirodhan.modules.business_admin.weight_pricing import WeightPricingConflict
from tirodhan.modules.identity.service import AuthenticatedPrincipal
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.routing_operations.admission import ReserveCapacity
from tirodhan.modules.routing_operations.authority import RouteAuthorityError
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.dispatch_models import RouteRevision, VehicleRoute
from tirodhan.modules.routing_operations.operational_commands import (
    AbortTransfer,
    AcceptAdmissionOffer,
    ObservePhysicalLoad,
    OperationalCommand,
    execute_operational,
)
from tirodhan.modules.routing_operations.orchestration_models import (
    AdmissionFlexibleCommitment,
    CapacityAdmissionRequest,
    CapacityReservation,
    FlexibleRouteOffer,
    RouteAcknowledgment,
    RouteDelivery,
)
from tirodhan.modules.routing_operations.slot_holds import HoldSlot, hold_slot
from tirodhan.modules.routing_operations.synchronization import customer_routing

router = APIRouter(prefix="/v2", tags=["regional orchestration"])
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Customer = Annotated[AuthenticatedPrincipal, Depends(require_role("CUSTOMER"))]
Rider = Annotated[AuthenticatedPrincipal, Depends(require_role("RIDER"))]
Manager = Annotated[AuthenticatedPrincipal, Depends(require_role("MANAGER"))]


class ReplacementSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_command_id: UUID
    selected_candidate_id: UUID
    expected_version: int = Field(strict=True, ge=1)
    expected_source_version: int = Field(strict=True, ge=1)
    expected_region_version: int = Field(strict=True, ge=1)
    expected_route_versions: dict[UUID, int]
    reason: str = Field(min_length=1, max_length=100, pattern=r"^[A-Z0-9_]+$")


@router.post("/manager/replacements/{replacement_id}/select")
async def select_replacement(
    replacement_id: UUID,
    body: ReplacementSelection,
    request: Request,
    actor: Manager,
    factory: Factory,
) -> dict[str, Any]:
    from tirodhan.modules.routing_operations.replacement_commands import DecideReplacement
    from tirodhan.modules.routing_operations.replacement_reads import read_replacement_options

    await _command(
        DecideReplacement(replacement_id=replacement_id, decision="APPROVE", **body.model_dump()),
        request,
        actor,
        factory,
    )
    async with factory() as session:
        return await read_replacement_options(session, replacement_id, utc_now())


@router.get("/manager/replacements")
async def replacements(
    actor: Manager,
    factory: Factory,
    incident_id: UUID | None = None,
    after_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    from tirodhan.modules.routing_operations.capacity_models import RiderReplacement

    async with factory() as session:
        query = select(RiderReplacement).order_by(RiderReplacement.replacement_id).limit(limit + 1)
        if incident_id:
            query = query.where(RiderReplacement.incident_id == incident_id)
        if after_id:
            query = query.where(RiderReplacement.replacement_id > after_id)
        rows = list(await session.scalars(query))
        return {
            "items": [
                {
                    "replacement_id": r.replacement_id,
                    "request_id": r.request_id,
                    "incident_id": r.incident_id,
                    "status": r.status,
                    "version": r.version,
                    "requested_at": r.requested_at,
                    "failure_code": r.failure_code,
                    "activation_id": r.activation_id,
                }
                for r in rows[:limit]
            ],
            "next_cursor": rows[limit - 1].replacement_id if len(rows) > limit else None,
        }


@router.get("/customer/slot-availability")
async def regional_slots(
    serviceability_context_id: UUID,
    actor: Customer,
    factory: Factory,
) -> dict[str, Any]:
    from tirodhan.modules.routing_operations.orchestration_models import RegionCell
    from tirodhan.modules.routing_operations.slot_projection import read_slots
    from tirodhan.modules.serviceability.models import ServiceabilityContext

    now = utc_now()
    async with factory() as session:
        context = await session.get(ServiceabilityContext, serviceability_context_id)
        if (
            context is None
            or context.user_id != actor.user_id
            or context.status != "SERVICEABLE"
            or context.expires_at <= now
        ):
            raise HTTPException(404, "SERVICEABILITY_CONTEXT_UNAVAILABLE")
        region = await session.get(RegionCell, context.cell_id)
        if region is None:
            raise HTTPException(409, "OPERATING_REGION_REQUIRED")
        from tirodhan.modules.routing_operations.orchestration_models import OperatingRegion

        operating = await session.get(OperatingRegion, region.region_id)
        assert operating is not None
        return {
            "region_id": region.region_id,
            "capacity_certified": False,
            "allowance_is_estimate": True,
            "standard_pickup_seconds": operating.policy["minimum_pickup_seconds"],
            "standard_discharge_seconds": operating.policy["minimum_discharge_seconds"],
            "slots": await read_slots(session, region.region_id, now=now),
        }


@router.get("/manager/replacements/{replacement_id}")
async def read_replacement(
    replacement_id: UUID,
    actor: Manager,
    factory: Factory,
    after_rank: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    from tirodhan.modules.routing_operations.replacement_reads import read_replacement_options

    async with factory() as session:
        try:
            return await read_replacement_options(
                session, replacement_id, utc_now(), after_rank=after_rank, limit=limit
            )
        except ValueError as error:
            raise HTTPException(404, str(error)) from None


@router.get("/manager/regions/{region_id}/capacity")
async def regional_capacity(
    region_id: UUID,
    actor: Manager,
    factory: Factory,
    after_vehicle_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    from tirodhan.modules.routing_operations.models import OperatingShift
    from tirodhan.modules.routing_operations.orchestration_models import (
        OperatingRegion,
        RegionVehicle,
    )

    async with factory() as session:
        region = await session.get(OperatingRegion, region_id)
        if region is None:
            raise HTTPException(404, "OPERATING_REGION_UNAVAILABLE")
        resources = (
            (
                await session.execute(
                    select(
                        RegionVehicle.vehicle_id,
                        RegionVehicle.version.label("membership_version"),
                        OperatingShift.shift_id,
                        OperatingShift.rider_id,
                        OperatingShift.starts_at,
                        OperatingShift.ends_at,
                    )
                    .outerjoin(
                        OperatingShift,
                        (OperatingShift.vehicle_id == RegionVehicle.vehicle_id)
                        & OperatingShift.closed_at.is_(None),
                    )
                    .where(
                        RegionVehicle.region_id == region_id,
                        RegionVehicle.vehicle_id > after_vehicle_id if after_vehicle_id else true(),
                    )
                    .order_by(RegionVehicle.vehicle_id)
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        return {
            "region_id": region_id,
            "region_version": region.version,
            "resources": [dict(r) for r in resources[:limit]],
            "next_cursor": resources[limit - 1]["vehicle_id"] if len(resources) > limit else None,
        }


@router.get("/rider/flexible-admission-offers", deprecated=True)
async def flexible_admission_offers(actor: Rider, factory: Factory) -> dict[str, Any]:
    raise HTTPException(410, "COMMERCIAL_ACCEPTANCE_RETIRED_USE_SLOT_HOLDS")


def _ttl(request: Request) -> timedelta:
    value = request.app.state.settings.command_idempotency_ttl_seconds
    if value is None:
        raise HTTPException(503, "COMMAND_RETENTION_REQUIRED")
    return timedelta(seconds=value)


@router.post("/customer/slot-holds", status_code=201)
async def reserve_slot(
    command: HoldSlot, request: Request, actor: Customer, factory: Factory
) -> dict[str, Any]:
    lifetime = request.app.state.settings.pending_payment_lifetime_seconds
    if lifetime is None:
        raise HTTPException(503, "PAYMENT_LIFETIME_REQUIRED")
    try:
        identity = await hold_slot(
            factory,
            customer_id=actor.user_id,
            command=command,
            expires_at=utc_now() + timedelta(seconds=lifetime * 2),
            customer_checkout=True,
        )
    except (
        OperationalInputError,
        WeightPricingConflict,
        IdempotencyKeyConflictError,
        IntegrityError,
    ) as error:
        raise HTTPException(
            409,
            str(error)
            if isinstance(error, (OperationalInputError, WeightPricingConflict))
            else "SLOT_HOLD_CONFLICT",
        ) from None
    return await slot_hold_status(identity, request, actor, factory)


@router.get("/customer/slot-holds/{reservation_id}")
async def slot_hold_status(
    reservation_id: UUID, request: Request, actor: Customer, factory: Factory
) -> dict[str, Any]:
    async with factory() as session:
        row = await session.get(CapacityReservation, reservation_id)
        if row is None or row.customer_id != actor.user_id:
            raise HTTPException(404, "SLOT_HOLD_UNAVAILABLE")
        lifetime = request.app.state.settings.pending_payment_lifetime_seconds
        if lifetime is None:
            raise HTTPException(503, "PAYMENT_LIFETIME_REQUIRED")
        book_by = row.expires_at - timedelta(seconds=lifetime)
        return dict(
            reservation_id=row.reservation_id,
            admission_basis=row.admission_basis,
            bound_request_id=row.request_id,
            bookable=row.request_id is None and utc_now() <= book_by,
            expires_at=row.expires_at,
            book_by=book_by,
        )


@router.post("/customer/capacity-admissions", status_code=202, deprecated=True)
async def prepare_admission(
    command: ReserveCapacity, request: Request, actor: Customer, factory: Factory
) -> dict[str, Any]:
    raise HTTPException(410, "COMMERCIAL_ACCEPTANCE_RETIRED_USE_SLOT_HOLDS")


@router.get("/customer/capacity-admissions/{admission_id}")
async def admission_status(
    admission_id: UUID, request: Request, actor: Customer, factory: Factory
) -> dict[str, Any]:
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, admission_id)
        if row is None or row.customer_id != actor.user_id:
            raise HTTPException(404, "ADMISSION_UNAVAILABLE")
        reservation = await session.get(CapacityReservation, admission_id)
        retired = reservation is None and row.status in ("PENDING", "RUNNING", "NEEDS_ACCEPTANCE")
        now = utc_now()
        available = reservation is not None and reservation.expires_at > now
        committed_flex = await session.get(AdmissionFlexibleCommitment, admission_id)
        book_by = (
            reservation.expires_at
            - timedelta(seconds=request.app.state.settings.pending_payment_lifetime_seconds)
            if reservation is not None
            else None
        )
        bookable = (
            available
            and book_by is not None
            and now <= book_by
            and reservation is not None
            and reservation.request_id is None
        )
        return {
            "admission_id": row.admission_id,
            "bookable": bookable,
            "status": "RETIRED" if retired else "SUCCEEDED" if reservation else row.status,
            "failure_code": "COMMERCIAL_ACCEPTANCE_RETIRED" if retired else row.failure_code,
            "reservation_id": reservation.reservation_id
            if bookable and reservation is not None
            else None,
            "expires_at": reservation.expires_at if available and reservation is not None else None,
            "capacity_class": "RETIRED"
            if retired
            else ("ACCEPTED_FLEXIBLE" if committed_flex else "CONFIRMED_COMMITTED")
            if available
            else "AWAITING_FLEXIBLE_ACCEPTANCE"
            if row.status == "NEEDS_ACCEPTANCE"
            else "UNCONFIRMED",
            "book_by": book_by,
        }


async def _command(
    command: OperationalCommand,
    request: Request,
    actor: AuthenticatedPrincipal,
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, UUID]:
    if isinstance(command, AcceptAdmissionOffer):
        raise HTTPException(410, "COMMERCIAL_ACCEPTANCE_RETIRED_USE_SLOT_HOLDS")
    try:
        result = await execute_operational(
            factory, actor_id=actor.user_id, command=command, expires_at=utc_now() + _ttl(request)
        )
    except (
        OperationalInputError,
        RouteAuthorityError,
        IdempotencyKeyConflictError,
        IntegrityError,
    ) as error:
        raise HTTPException(
            409,
            str(error)
            if isinstance(error, (OperationalInputError, RouteAuthorityError))
            else "COMMAND_CONFLICT",
        ) from None
    response = {"resource_id": result}
    if isinstance(command, (ObservePhysicalLoad, AbortTransfer)):
        from tirodhan.modules.routing_operations.orchestration_models import (
            CargoTransferIntent,
            PhysicalLoadObservation,
        )

        async with factory() as session:
            if isinstance(command, ObservePhysicalLoad):
                observation = await session.get(PhysicalLoadObservation, result)
                assert observation is not None
                response["evidence_capture_id"] = observation.capture_id
            else:
                intent = await session.get(CargoTransferIntent, result)
                assert intent is not None and intent.resolution_evidence_id is not None
                response["evidence_capture_id"] = intent.resolution_evidence_id
    return response


@router.post("/manager/operations")
async def manager_operation(
    command: OperationalCommand, request: Request, actor: Manager, factory: Factory
) -> dict[str, UUID]:
    return await _command(command, request, actor, factory)


@router.get("/manager/replacements/{replacement_id}/evaluations")
async def replacement_evaluations(
    replacement_id: UUID,
    actor: Manager,
    factory: Factory,
    after_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    from tirodhan.modules.routing_operations.replacement_reads import evaluation_history

    async with factory() as session:
        try:
            return await evaluation_history(session, replacement_id, after_id=after_id, limit=limit)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None


@router.get("/manager/replacements/{replacement_id}/evaluations/{evaluation_id}")
async def replacement_evaluation(
    replacement_id: UUID,
    evaluation_id: UUID,
    actor: Manager,
    factory: Factory,
    after_rank: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    from tirodhan.modules.routing_operations.replacement_reads import evaluation_history

    async with factory() as session:
        try:
            return await evaluation_history(
                session,
                replacement_id,
                evaluation_id=evaluation_id,
                after_rank=after_rank,
                limit=limit,
            )
        except ValueError as error:
            raise HTTPException(404, str(error)) from None


@router.post("/rider/operations")
async def rider_operation(
    command: OperationalCommand, request: Request, actor: Rider, factory: Factory
) -> dict[str, UUID]:
    return await _command(command, request, actor, factory)


@router.post("/customer/operations")
async def customer_operation(
    command: OperationalCommand, request: Request, actor: Customer, factory: Factory
) -> dict[str, UUID]:
    return await _command(command, request, actor, factory)


@router.get("/customer/collection-requests/{request_id}/routing")
async def customer_status(request_id: UUID, actor: Customer, factory: Factory) -> dict[str, Any]:
    try:
        return await customer_routing(
            factory, customer_id=actor.user_id, request_id=request_id, now=utc_now()
        )
    except RouteAuthorityError:
        raise HTTPException(404, "BOOKING_UNAVAILABLE") from None


@router.get("/rider/dispatch-sync")
async def dispatch_sync(actor: Rider, factory: Factory) -> dict[str, Any]:
    from tirodhan.modules.routing_operations.models import OperatingShift

    async with factory() as session:
        rows = (
            await session.execute(
                select(RouteRevision, RouteDelivery)
                .join(VehicleRoute)
                .join(OperatingShift, OperatingShift.shift_id == VehicleRoute.shift_id)
                .outerjoin(RouteDelivery, RouteDelivery.revision_id == RouteRevision.revision_id)
                .where(
                    OperatingShift.rider_id == actor.user_id,
                    VehicleRoute.status != "CLOSED",
                    RouteRevision.version == VehicleRoute.version,
                )
            )
        ).all()
        return {
            "revisions": [
                {
                    "route_id": revision.route_id,
                    "revision_id": revision.revision_id,
                    "version": revision.version,
                    "acknowledged": await session.get(RouteAcknowledgment, revision.revision_id)
                    is not None,
                    "acknowledgment_due_at": delivery.acknowledgment_due_at if delivery else None,
                    "receipt_is_acceptance": False,
                }
                for revision, delivery in rows
            ]
        }


@router.get("/rider/flexible-route-offers")
async def flexible_offers(actor: Rider, factory: Factory) -> dict[str, Any]:
    from tirodhan.modules.routing_operations.models import OperatingShift

    async with factory() as session:
        offers = list(
            await session.scalars(
                select(FlexibleRouteOffer)
                .join(OperatingShift)
                .where(
                    OperatingShift.rider_id == actor.user_id,
                    FlexibleRouteOffer.expires_at > utc_now(),
                    FlexibleRouteOffer.accepted_at.is_(None),
                )
                .limit(100)
            )
        )
        return {
            "offers": [
                {
                    "offer_id": o.offer_id,
                    "request_id": o.request_id,
                    "expires_at": o.expires_at,
                    "assignment_authorized": False,
                }
                for o in offers
            ]
        }
