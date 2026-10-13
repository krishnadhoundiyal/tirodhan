from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import exists, select

from tirodhan.api.routes.routing_dispatch import (
    CONFLICTS,
    Factory,
    Manager,
    Rider,
    _conflict,
    _expiry,
)
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAssignmentItem
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.routing_operations.fleet_models import (
    FleetControl,
    FleetCycle,
    FleetException,
)
from tirodhan.modules.routing_operations.models import RoutingManifest
from tirodhan.modules.routing_operations.rolling import (
    SCOPE,
    ConfigureFleet,
    FleetPolicy,
    configure_fleet,
)
from tirodhan.modules.routing_operations.telemetry import (
    PositionCommand,
    VehicleIssueCommand,
    report_position,
    report_vehicle_issue,
)

router = APIRouter(prefix="/v2", tags=["continuous committed fleet"])


@router.get("/manager/fleet/backlog")
async def backlog(
    principal: Manager,
    factory: Factory,
    after_request_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    owned = exists().where(
        PickupExecution.request_id == CollectionRequest.request_id,
        RiderAssignmentItem.pickup_execution_id == PickupExecution.pickup_execution_id,
        RiderAssignmentItem.released_at.is_(None),
    )
    disputed = exists().where(
        FleetException.request_id == CollectionRequest.request_id,
        FleetException.reason == "LOAD_UNCERTAIN",
    )
    query = select(
        CollectionRequest,
        (
            exists().where(RoutingManifest.request_id == CollectionRequest.request_id) & ~disputed
        ).label("manifest_ready"),
        disputed.label("load_uncertain"),
    ).where(CollectionRequest.status.in_(("ACCEPTED", "PRE_PLANNING", "PLANNED")), ~owned)
    if after_request_id is not None:
        query = query.where(CollectionRequest.request_id > after_request_id)
    async with factory() as session:
        rows = (
            await session.execute(query.order_by(CollectionRequest.request_id).limit(limit + 1))
        ).all()
    return {
        "items": [
            {
                "request_id": r.request_id,
                "status": r.status,
                "slot_start": r.slot_start,
                "slot_end": r.slot_end,
                "manifest_ready": ready,
                "load_uncertain": uncertain,
            }
            for r, ready, uncertain in rows[:limit]
        ],
        "next_request_id": rows[limit - 1][0].request_id if len(rows) > limit else None,
    }


@router.post("/manager/fleet/policy")
async def configure(
    command: ConfigureFleet,
    request: Request,
    principal: Manager,
    factory: Factory,
) -> dict[str, Any]:
    try:
        result = await configure_fleet(
            factory, manager_id=principal.user_id, command=command, expires_at=_expiry(request)
        )
    except CONFLICTS as error:
        raise _conflict(error) from None
    return {"command_id": result}


@router.get("/manager/fleet/status")
async def status(principal: Manager, factory: Factory) -> dict[str, Any]:
    async with factory() as session:
        control = await session.get(FleetControl, SCOPE)
        cycles = list(
            await session.scalars(
                select(FleetCycle)
                .order_by(FleetCycle.created_at.desc(), FleetCycle.cycle_id.desc())
                .limit(50)
            )
        )
        exceptions = list(
            await session.scalars(
                select(FleetException)
                .order_by(FleetException.created_at.desc(), FleetException.exception_id.desc())
                .limit(100)
            )
        )
        return {
            "policy_version": control.version if control else None,
            "next_due_at": control.next_due_at if control else None,
            "cycles": [
                {
                    "cycle_id": c.cycle_id,
                    "status": c.status,
                    "failure_code": c.failure_code,
                    "activation_id": c.activation_id,
                    "measurements": c.measurements,
                }
                for c in cycles
            ],
            "exceptions": [
                {
                    "exception_id": e.exception_id,
                    "request_id": e.request_id,
                    "route_id": e.route_id,
                    "reason": e.reason,
                    "created_at": e.created_at,
                }
                for e in exceptions
            ],
        }


@router.post("/rider/vehicle-position")
async def position(
    command: PositionCommand,
    request: Request,
    principal: Rider,
    factory: Factory,
) -> dict[str, Any]:
    async with factory() as session:
        control = await session.get(FleetControl, SCOPE)
        if control is None:
            raise HTTPException(503, "Fleet telemetry policy is not configured")
        policy = FleetPolicy.model_validate(control.configuration).telemetry
    try:
        result = await report_position(
            factory,
            rider_id=principal.user_id,
            session_id=principal.refresh_session_id,
            command=command,
            policy=policy,
            expires_at=_expiry(request),
        )
    except CONFLICTS as error:
        raise _conflict(error) from None
    return {"position_id": result}


@router.post("/rider/vehicle-issue")
async def issue(
    command: VehicleIssueCommand,
    request: Request,
    principal: Rider,
    factory: Factory,
) -> dict[str, Any]:
    try:
        result = await report_vehicle_issue(
            factory, rider_id=principal.user_id, command=command, expires_at=_expiry(request)
        )
    except CONFLICTS as error:
        raise _conflict(error) from None
    return {"exception_id": result}
