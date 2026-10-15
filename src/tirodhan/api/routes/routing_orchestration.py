"""Default-disabled Increment 7 contracts; native planning stays in finite workers."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_session_factory, require_role
from tirodhan.db.values import utc_now
from tirodhan.modules.identity.service import AuthenticatedPrincipal
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.routing_operations.admission import ReserveCapacity
from tirodhan.modules.routing_operations.admission_jobs import request_admission
from tirodhan.modules.routing_operations.authority import RouteAuthorityError
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.dispatch_models import RouteRevision, VehicleRoute
from tirodhan.modules.routing_operations.operational_commands import (
    AbortTransfer,
    ObservePhysicalLoad,
    OperationalCommand,
    execute_operational,
)
from tirodhan.modules.routing_operations.orchestration_models import (
    AdmissionFlexibleCommitment,
    AdmissionFlexibleOffer,
    CapacityAdmissionRequest,
    CapacityReservation,
    FlexibleRouteOffer,
    RouteAcknowledgment,
    RouteDelivery,
)
from tirodhan.modules.routing_operations.synchronization import customer_routing

router = APIRouter(prefix="/v2", tags=["regional orchestration"])
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Customer = Annotated[AuthenticatedPrincipal, Depends(require_role("CUSTOMER"))]
Rider = Annotated[AuthenticatedPrincipal, Depends(require_role("RIDER"))]
Manager = Annotated[AuthenticatedPrincipal, Depends(require_role("MANAGER"))]


@router.get("/rider/flexible-admission-offers")
async def flexible_admission_offers(actor: Rider, factory: Factory) -> dict[str, Any]:
    from tirodhan.modules.routing_operations.models import OperatingShift

    async with factory() as session:
        offers = list(
            await session.scalars(
                select(AdmissionFlexibleOffer)
                .join(OperatingShift)
                .join(CapacityAdmissionRequest)
                .where(
                    OperatingShift.rider_id == actor.user_id,
                    OperatingShift.closed_at.is_(None),
                    CapacityAdmissionRequest.status == "NEEDS_ACCEPTANCE",
                    AdmissionFlexibleOffer.expires_at > utc_now(),
                )
                .order_by(AdmissionFlexibleOffer.expires_at, AdmissionFlexibleOffer.offer_id)
                .limit(32)
            )
        )
        return {
            "offers": [
                {
                    "offer_id": o.offer_id,
                    "shift_id": o.shift_id,
                    "admission_id": o.admission_id,
                    "expires_at": o.expires_at,
                    "acceptance_required": True,
                    "capacity_confirmed": False,
                }
                for o in offers
            ]
        }


def _ttl(request: Request) -> timedelta:
    value = request.app.state.settings.command_idempotency_ttl_seconds
    if value is None:
        raise HTTPException(503, "COMMAND_RETENTION_REQUIRED")
    return timedelta(seconds=value)


@router.post("/customer/capacity-admissions", status_code=202)
async def prepare_admission(
    command: ReserveCapacity, request: Request, actor: Customer, factory: Factory
) -> dict[str, Any]:
    lifetime = request.app.state.settings.pending_payment_lifetime_seconds
    if lifetime is None:
        raise HTTPException(503, "PAYMENT_LIFETIME_REQUIRED")
    try:
        row = await request_admission(
            factory,
            customer_id=actor.user_id,
            command=command,
            expires_at=utc_now() + timedelta(seconds=lifetime * 2),
        )
    except (
        OperationalInputError,
        RouteAuthorityError,
        IdempotencyKeyConflictError,
        IntegrityError,
    ):
        raise HTTPException(409, "ADMISSION_CONFLICT") from None
    return {"admission_id": row.admission_id, "status": row.status}


@router.get("/customer/capacity-admissions/{admission_id}")
async def admission_status(
    admission_id: UUID, request: Request, actor: Customer, factory: Factory
) -> dict[str, Any]:
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, admission_id)
        if row is None or row.customer_id != actor.user_id:
            raise HTTPException(404, "ADMISSION_UNAVAILABLE")
        reservation = await session.get(CapacityReservation, admission_id)
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
            "status": row.status,
            "failure_code": row.failure_code,
            "reservation_id": reservation.reservation_id
            if bookable and reservation is not None
            else None,
            "expires_at": reservation.expires_at if available and reservation is not None else None,
            "capacity_class": ("ACCEPTED_FLEXIBLE" if committed_flex else "CONFIRMED_COMMITTED")
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
