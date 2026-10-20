"""Manager-only joins over existing operational history; no household PII."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem
from tirodhan.modules.pickups.models import PickupAttempt
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.routing_operations.capacity_models import (
    ReplacementDecision,
    RiderReplacement,
)
from tirodhan.modules.routing_operations.dispatch_models import (
    RouteActivation,
    RouteRevision,
    RouteRevisionStop,
    RouteTask,
    VehicleRoute,
)
from tirodhan.modules.routing_operations.orchestration_models import CargoTransfer


def row_state(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


async def booking_facts(
    session: AsyncSession,
    request_id: UUID,
    kind: str,
    *,
    after_id: UUID | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    pickup_id = await session.scalar(
        select(PickupExecution.pickup_execution_id).where(PickupExecution.request_id == request_id)
    )
    if pickup_id is None:
        raise ValueError("BOOKING_EXECUTION_UNAVAILABLE")
    choices = {
        "attempts": (
            PickupAttempt,
            "pickup_attempt_id",
            PickupAttempt.pickup_execution_id == pickup_id,
        ),
        "replacements": (
            RiderReplacement,
            "replacement_id",
            RiderReplacement.request_id == request_id,
        ),
        "decisions": (
            ReplacementDecision,
            "decision_id",
            ReplacementDecision.replacement_id.in_(
                select(RiderReplacement.replacement_id).where(
                    RiderReplacement.request_id == request_id
                )
            ),
        ),
        "transfers": (CargoTransfer, "transfer_id", CargoTransfer.request_id == request_id),
    }
    if kind not in choices:
        raise ValueError("BOOKING_HISTORY_UNAVAILABLE")
    model, key, predicate = choices[kind]
    column = getattr(model, key)
    query = select(model).where(predicate)
    if after_id:
        query = query.where(column > after_id)
    rows = list(await session.scalars(query.order_by(column).limit(limit + 1)))
    return {
        "items": [row_state(r) for r in rows[:limit]],
        "next_cursor": getattr(rows[limit - 1], key) if len(rows) > limit else None,
    }


async def booking_lineage(
    session: AsyncSession,
    request_id: UUID,
    *,
    after_id: UUID | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    pickup = await session.scalar(
        select(PickupExecution).where(PickupExecution.request_id == request_id)
    )
    if pickup is None:
        raise ValueError("BOOKING_EXECUTION_UNAVAILABLE")
    query = (
        select(RiderAssignment, RiderAssignmentItem, VehicleRoute)
        .join(
            RiderAssignmentItem, RiderAssignmentItem.assignment_id == RiderAssignment.assignment_id
        )
        .outerjoin(VehicleRoute, VehicleRoute.assignment_id == RiderAssignment.assignment_id)
        .where(RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id)
    )
    if after_id:
        query = query.where(RiderAssignment.assignment_id > after_id)
    rows = (
        await session.execute(query.order_by(RiderAssignment.assignment_id).limit(limit + 1))
    ).all()
    assignments = [
        {
            "assignment_id": a.assignment_id,
            "rider_id": a.rider_id,
            "status": a.status,
            "assigned_at": a.assigned_at,
            "started_at": a.started_at,
            "completed_at": a.completed_at,
            "supersedes_assignment_id": a.supersedes_assignment_id,
            "released_at": i.released_at,
            "release_reason_code": i.release_reason_code,
            "route_id": r.route_id if r else None,
            "vehicle_id": r.vehicle_id if r else None,
            "current_route_version": r.version if r else None,
        }
        for a, i, r in rows[:limit]
    ]
    attempts = list(
        await session.scalars(
            select(PickupAttempt)
            .where(PickupAttempt.pickup_execution_id == pickup.pickup_execution_id)
            .order_by(PickupAttempt.attempt_number)
            .limit(100)
        )
    )
    replacements = list(
        await session.scalars(
            select(RiderReplacement)
            .where(RiderReplacement.request_id == request_id)
            .order_by(RiderReplacement.requested_at, RiderReplacement.replacement_id)
            .limit(100)
        )
    )
    decisions = list(
        await session.scalars(
            select(ReplacementDecision)
            .where(ReplacementDecision.replacement_id.in_([r.replacement_id for r in replacements]))
            .order_by(ReplacementDecision.decided_at, ReplacementDecision.decision_id)
            .limit(200)
        )
    )
    transfers = list(
        await session.scalars(
            select(CargoTransfer)
            .where(CargoTransfer.request_id == request_id)
            .order_by(CargoTransfer.sequence)
            .limit(100)
        )
    )
    return {
        "request_id": request_id,
        "pickup_execution_id": pickup.pickup_execution_id,
        "pickup_status": pickup.status,
        "assignments": assignments,
        "pickup_attempts": [row_state(a) for a in attempts],
        "replacements": [row_state(r) for r in replacements],
        "decisions": [row_state(d) for d in decisions],
        "custody_transfers": [row_state(t) for t in transfers],
        "next_cursor": rows[limit - 1][0].assignment_id if len(rows) > limit else None,
        "related_history_limits": {
            "attempts": 100,
            "replacements": 100,
            "decisions": 200,
            "transfers": 100,
        },
    }


async def route_history(
    session: AsyncSession, route_id: UUID, *, after: int = 0, limit: int = 50
) -> dict[str, Any]:
    root = await session.get(VehicleRoute, route_id)
    if root is None:
        raise ValueError("ROUTE_UNAVAILABLE")
    rows = (
        await session.execute(
            select(RouteRevision, RouteActivation)
            .join(RouteActivation)
            .where(RouteRevision.route_id == route_id, RouteRevision.version > after)
            .order_by(RouteRevision.version)
            .limit(limit + 1)
        )
    ).all()
    versions = []
    for revision, activation in rows[:limit]:
        stops = (
            await session.execute(
                select(RouteRevisionStop, RouteTask)
                .join(RouteTask)
                .where(RouteRevisionStop.revision_id == revision.revision_id)
                .order_by(RouteRevisionStop.ordinal)
            )
        ).all()
        versions.append(
            {
                "revision_id": revision.revision_id,
                "version": revision.version,
                "supersedes_revision_id": revision.supersedes_revision_id,
                "is_current": revision.version == root.version,
                "created_at": revision.created_at,
                "actor_id": activation.activated_by,
                "activation_id": activation.activation_id,
                "run_id": activation.run_id,
                "stops": [
                    {
                        "task_id": t.task_id,
                        "kind": t.kind,
                        "pickup_execution_id": t.pickup_execution_id,
                        "ordinal": s.ordinal,
                        "planned_start": s.planned_start,
                        "planned_end": s.planned_end,
                        "current_task_state": t.state,
                    }
                    for s, t in stops
                ],
            }
        )
    return {
        "route_id": route_id,
        "assignment_id": root.assignment_id,
        "vehicle_id": root.vehicle_id,
        "current_version": root.version,
        "versions": versions,
        "next_cursor": rows[limit - 1][0].version if len(rows) > limit else None,
    }
