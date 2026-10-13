from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_session_factory, require_role
from tirodhan.db.values import utc_now
from tirodhan.modules.dispatch.models import RiderAssignment
from tirodhan.modules.handovers.models import HandoverEvent
from tirodhan.modules.handovers.service import HandoverError
from tirodhan.modules.identity.service import AuthenticatedPrincipal
from tirodhan.modules.planning.routing.validation import RoutingValidationError
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.routing_operations.authority import ActivationCommand, activate_proposal
from tirodhan.modules.routing_operations.commands import OperationalInputError, require_live_role
from tirodhan.modules.routing_operations.dispatch_models import (
    RouteRevision,
    RouteRevisionStop,
    RouteTask,
    VehicleRoute,
)
from tirodhan.modules.routing_operations.execution import (
    DischargeTask,
    ExecutionCommand,
    execute_task,
)
from tirodhan.modules.routing_operations.recovery import RecoveryCommand, recover_route

router = APIRouter(prefix="/v2", tags=["vehicle routes"])
Manager = Annotated[AuthenticatedPrincipal, Depends(require_role("MANAGER"))]
Rider = Annotated[AuthenticatedPrincipal, Depends(require_role("RIDER"))]
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
CONFLICTS = (
    HandoverError,
    OperationalInputError,
    RoutingValidationError,
    IdempotencyKeyConflictError,
    IntegrityError,
)


def _expiry(request: Request) -> datetime:
    ttl = request.app.state.settings.command_idempotency_ttl_seconds
    if ttl is None or ttl <= 0:
        raise HTTPException(503, "Command idempotency is not configured")
    return utc_now() + timedelta(seconds=ttl)


def _conflict(error: Exception) -> HTTPException:
    code = (
        str(error)
        if isinstance(error, (OperationalInputError, RoutingValidationError))
        else "COMMAND_CONFLICT"
    )
    return HTTPException(409, code)


@router.post("/manager/routes/activate")
async def activate(
    command: ActivationCommand, request: Request, principal: Manager, factory: Factory
) -> dict[str, Any]:
    try:
        result = await activate_proposal(
            factory, manager_id=principal.user_id, command=command, expires_at=_expiry(request)
        )
    except CONFLICTS as error:
        raise _conflict(error) from None
    async with factory() as session:
        rows = (
            await session.execute(
                select(RouteRevision, VehicleRoute)
                .join(VehicleRoute, VehicleRoute.route_id == RouteRevision.route_id)
                .where(RouteRevision.activation_id == result)
                .order_by(VehicleRoute.vehicle_id)
            )
        ).all()
    return {
        "activation_id": result,
        "routes": [
            {
                "route_id": root.route_id,
                "vehicle_id": root.vehicle_id,
                "assignment_id": root.assignment_id,
                "revision_id": revision.revision_id,
                "version": revision.version,
            }
            for revision, root in rows
        ],
    }


@router.post("/manager/routes/recover")
async def recover(
    command: RecoveryCommand, request: Request, principal: Manager, factory: Factory
) -> dict[str, Any]:
    try:
        result = await recover_route(
            factory, manager_id=principal.user_id, command=command, expires_at=_expiry(request)
        )
    except CONFLICTS as error:
        raise _conflict(error) from None
    return {"recovery_id": result}


@router.post("/rider/route-tasks")
async def progress(
    command: ExecutionCommand, request: Request, principal: Rider, factory: Factory
) -> dict[str, Any]:
    try:
        result = await execute_task(
            factory, rider_id=principal.user_id, command=command, expires_at=_expiry(request)
        )
    except CONFLICTS as error:
        raise _conflict(error) from None
    if isinstance(command, DischargeTask):
        async with factory() as session:
            event = await session.get(HandoverEvent, result)
            assert event is not None
            return {
                "resource_id": result,
                "handover_status": event.status,
                "validation_code": event.validation_code,
            }
    return {"resource_id": result}


@router.get("/rider/route")
async def read_route(principal: Rider, factory: Factory) -> dict[str, Any] | None:
    async with factory() as session:
        await require_live_role(session, principal.user_id, "RIDER")
        root = await session.scalar(
            select(VehicleRoute)
            .join(RiderAssignment, RiderAssignment.assignment_id == VehicleRoute.assignment_id)
            .where(RiderAssignment.rider_id == principal.user_id, VehicleRoute.status != "CLOSED")
        )
        if root is None:
            return None
        return await _route_view(session, root)


@router.get("/manager/routes/{route_id}")
async def manager_route(route_id: UUID, principal: Manager, factory: Factory) -> dict[str, Any]:
    async with factory() as session:
        await require_live_role(session, principal.user_id, "MANAGER")
        root = await session.get(VehicleRoute, route_id)
        if root is None:
            raise HTTPException(404, "ROUTE_UNAVAILABLE")
        return await _route_view(session, root)


async def _route_view(session: AsyncSession, root: VehicleRoute) -> dict[str, Any]:
    revision = await session.scalar(
        select(RouteRevision).where(
            RouteRevision.route_id == root.route_id, RouteRevision.version == root.version
        )
    )
    assert revision is not None
    rows = (
        await session.execute(
            select(RouteTask, RouteRevisionStop)
            .join(RouteRevisionStop, RouteRevisionStop.task_id == RouteTask.task_id)
            .where(RouteRevisionStop.revision_id == revision.revision_id)
            .order_by(RouteRevisionStop.ordinal)
        )
    ).all()
    return {
        "route_id": root.route_id,
        "vehicle_id": root.vehicle_id,
        "assignment_id": root.assignment_id,
        "revision_id": revision.revision_id,
        "status": root.status,
        "tasks": [
            {
                "task_id": task.task_id,
                "pickup_execution_id": task.pickup_execution_id,
                "kind": task.kind,
                "state": task.state,
                "committed_at": task.committed_at,
                "receiving_point_id": task.receiving_point_id,
                "planned_start": schedule.planned_start,
                "planned_end": schedule.planned_end,
            }
            for task, schedule in rows
        ],
    }
