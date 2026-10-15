"""Receipt is not voluntary acceptance; customer ETA is advisory and revision-scoped."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAssignmentItem
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.planning.routing.contracts import RouteProposal, RoutingProblem, StopKind
from tirodhan.modules.planning.routing.validation import validate_proposal
from tirodhan.modules.reliability.primitives import append_outbox_event

from .authority import RouteAuthorityError
from .dispatch_models import RouteRevision, VehicleRoute
from .orchestration_models import CapacityReservation, CustomerRouteETA


async def publish_etas(
    factory: async_sessionmaker[AsyncSession],
    problem: RoutingProblem,
    proposal: RouteProposal,
    *,
    now: datetime,
    freshness_seconds: int,
) -> None:
    async with factory() as reader:
        held = set(
            await reader.scalars(
                select(CapacityReservation.reservation_id).where(
                    CapacityReservation.reservation_id.in_([lot.lot_id for lot in problem.lots])
                )
            )
        )
    from .hold_projection import executable_projection

    problem, proposal = executable_projection(problem, proposal, held)
    assessment = validate_proposal(problem, proposal)
    stops = {s.stop_id: s for s in problem.stops}
    vehicles = {v.vehicle_id: v for v in problem.vehicles}
    async with factory() as session, session.begin():
        for route in assessment.routes:
            root = await session.scalar(
                select(VehicleRoute)
                .where(VehicleRoute.vehicle_id == route.vehicle_id, VehicleRoute.status == "ACTIVE")
                .with_for_update()
            )
            if root is None:
                continue
            revision = await session.scalar(
                select(RouteRevision).where(
                    RouteRevision.route_id == root.route_id, RouteRevision.version == root.version
                )
            )
            assert revision is not None
            from .execution import current_tasks

            current_order = tuple(t.stop_id for t in await current_tasks(session, root))
            candidate = next(r for r in proposal.routes if r.vehicle_id == route.vehicle_id)
            if candidate.stop_ids != current_order:
                continue  # Recommendations have no authority to change the current ETA order.
            current = set(current_order)
            for planned in route.stops:
                stop = stops[planned.stop_id]
                if stop.kind != StopKind.PICKUP or stop.stop_id not in current:
                    continue
                eta = problem.epoch + timedelta(seconds=planned.service_start_at)
                row = await session.get(CustomerRouteETA, stop.lot_id)
                material = (
                    row is None
                    or row.revision_id != revision.revision_id
                    or abs((row.service_start_at - eta).total_seconds()) >= 60
                )
                if row is None:
                    row = CustomerRouteETA(request_id=stop.lot_id)
                    session.add(row)
                row.revision_id, row.rider_id, row.service_start_at = (
                    revision.revision_id,
                    vehicles[route.vehicle_id].rider_id,
                    eta,
                )
                row.calculated_at, row.fresh_until = now, now + timedelta(seconds=freshness_seconds)
                if material:
                    await append_outbox_event(
                        session,
                        event_key=f"customer-route-eta:{stop.lot_id}:{revision.revision_id}:{now.isoformat()}",
                        aggregate_type="CollectionRequest",
                        aggregate_id=stop.lot_id,
                        event_type="CustomerRoutingStatusChanged",
                        payload={"request_id": str(stop.lot_id)},
                    )


async def customer_routing(
    factory: async_sessionmaker[AsyncSession], *, customer_id: UUID, request_id: UUID, now: datetime
) -> dict[str, Any]:
    async with factory() as session:
        booking = await session.get(CollectionRequest, request_id)
        if booking is None or booking.customer_id != customer_id:
            raise RouteAuthorityError("BOOKING_UNAVAILABLE")
        if booking.status in ("CANCELLED", "EXPIRED", "COMPLETED"):
            return {"status": booking.status, "assigned_rider_id": None, "eta": None}
        from .operational_commands import custodian
        from .physical import appointment_window

        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == request_id)
        )
        window = await appointment_window(session, request_id, booking.slot_start, booking.slot_end)
        if pickup is not None and pickup.status == "COLLECTED":
            _, rider = await custodian(session, request_id)
            return {
                "status": "IN_TRANSIT",
                "assigned_rider_id": rider,
                "eta": None,
                "service_window_start": window[0],
                "service_window_end": window[1],
            }
        root = await session.scalar(
            select(VehicleRoute)
            .join(
                RiderAssignmentItem, RiderAssignmentItem.assignment_id == VehicleRoute.assignment_id
            )
            .join(PickupExecution)
            .where(
                PickupExecution.request_id == request_id,
                RiderAssignmentItem.released_at.is_(None),
                VehicleRoute.status != "CLOSED",
            )
        )
        if root is None:
            if now > window[1]:
                return {
                    "status": "APPOINTMENT_REQUIRES_REVIEW",
                    "assigned_rider_id": None,
                    "eta": None,
                    "service_window_start": window[0],
                    "service_window_end": window[1],
                }
            return {
                "status": "UNASSIGNED",
                "assigned_rider_id": None,
                "eta": None,
                "service_window_start": window[0],
                "service_window_end": window[1],
            }
        from .models import OperatingShift

        shift = await session.get(OperatingShift, root.shift_id)
        assert shift is not None
        row = await session.get(CustomerRouteETA, request_id)
        revision = await session.get(RouteRevision, row.revision_id) if row else None
        fresh = (
            row is not None
            and revision is not None
            and revision.route_id == root.route_id
            and revision.version == root.version
            and now < row.fresh_until
            and root.status == "ACTIVE"
        )
        return {
            "service_window_start": window[0],
            "service_window_end": window[1],
            "status": "ASSIGNED" if root.status == "ACTIVE" else "RECOVERY_REQUIRED",
            "assigned_rider_id": shift.rider_id,
            "eta": row.service_start_at if fresh and row is not None else None,
            "eta_state": "ESTIMATED" if fresh else "UPDATING",
            "calculated_at": row.calculated_at if row else None,
            "revision_id": revision.revision_id if fresh and revision is not None else None,
        }
