from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_database_session, get_session_factory, require_role
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.service import (
    CollectionRequestCompletionError,
    CollectionRequestNotFoundError,
    complete_collection_request,
)
from tirodhan.modules.dispatch.fleet_manager import (
    FleetManagerError,
    activate_fleet_cell,
    activate_rider_cell,
    add_rider_to_fleet,
    create_fleet,
    deactivate_fleet_cell,
    deactivate_rider_cell,
    end_rider_fleet_membership,
    set_fleet_status,
)
from tirodhan.modules.dispatch.models import RiderAssignment
from tirodhan.modules.dispatch.queries import (
    ManagerRiderRead,
    PendingGroupRead,
    list_manager_riders,
    list_pending_assignment_groups,
)
from tirodhan.modules.dispatch.service import (
    AssignmentStateInconsistentError,
    CollectionGroupNotFoundError,
    GroupAlreadyAssignedError,
    PickupGroupNotAssignableError,
    RiderNotEligibleError,
    RiderNotFoundError,
    assign_group_manually,
)
from tirodhan.modules.identity.service import ROLE_MANAGER, AuthenticatedPrincipal
from tirodhan.modules.operations.queries import list_open_incidents
from tirodhan.modules.operations.service import (
    PickupIncidentConflictError,
    PickupIncidentNotFoundError,
    PickupIncidentStateError,
    ReassignmentCommandInProgressError,
    ReassignmentConflictError,
    ReassignmentNotFoundError,
    ReassignmentRiderError,
    ReassignmentStateError,
    reassign_outstanding_work,
)
from tirodhan.modules.pickups.models import PickupIncident
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

router = APIRouter(prefix="/v1/manager", tags=["manager"])


class ManagerCommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManagerRiderResponse(BaseModel):
    rider_id: UUID
    status: str
    vehicle_type_code: str | None
    capacity_class_code: str | None
    availability_intent: str
    work_state: str
    version: int
    updated_at: datetime


class PendingGroupResponse(BaseModel):
    collection_group_id: UUID
    cell_id: str
    planning_mode: str
    slot_start: datetime
    slot_end: datetime
    pickup_count: int
    created_at: datetime


class AssignmentSummaryResponse(BaseModel):
    assignment_id: UUID
    collection_group_id: UUID
    rider_id: UUID
    source: str
    status: str
    assigned_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ManagerIncidentResponse(BaseModel):
    incident_id: UUID
    pickup_execution_id: UUID
    assignment_id: UUID
    reason_code: str
    status: str
    opened_at: datetime


class CompletionResponse(BaseModel):
    request_id: UUID
    status: str
    completed_at: datetime | None


class ManualAssignmentRequest(ManagerCommandModel):
    rider_id: UUID


class ReassignmentRequest(ManagerCommandModel):
    replacement_rider_id: UUID
    client_reassignment_id: UUID
    incident_id: UUID | None = None


def _rider_response(value: ManagerRiderRead) -> ManagerRiderResponse:
    return ManagerRiderResponse(
        rider_id=value.rider_id,
        status="ACTIVE",
        vehicle_type_code=value.vehicle_type_code,
        capacity_class_code=value.capacity_class_code,
        availability_intent=value.availability_intent,
        work_state=value.work_state,
        version=value.version,
        updated_at=value.updated_at,
    )


def _pending_group_response(value: PendingGroupRead) -> PendingGroupResponse:
    return PendingGroupResponse(
        collection_group_id=value.collection_group_id,
        cell_id=value.cell_id,
        planning_mode=value.planning_mode,
        slot_start=value.slot_start,
        slot_end=value.slot_end,
        pickup_count=value.pickup_count,
        created_at=value.created_at,
    )


def _assignment_response(value: RiderAssignment) -> AssignmentSummaryResponse:
    return AssignmentSummaryResponse(
        assignment_id=value.assignment_id,
        collection_group_id=value.collection_group_id,
        rider_id=value.rider_id,
        source=value.source,
        status=value.status,
        assigned_at=value.assigned_at,
        started_at=value.started_at,
        completed_at=value.completed_at,
    )


def _incident_response(value: PickupIncident) -> ManagerIncidentResponse:
    return ManagerIncidentResponse(
        incident_id=value.incident_id,
        pickup_execution_id=value.pickup_execution_id,
        assignment_id=value.assignment_id,
        reason_code=value.reason_code,
        status=value.status,
        opened_at=value.opened_at,
    )


def _completion_response(value: CollectionRequest) -> CompletionResponse:
    return CompletionResponse(
        request_id=value.request_id,
        status=value.status,
        completed_at=value.completed_at,
    )


def _idempotency_expiry(request: Request) -> datetime:
    settings = cast(Settings, request.app.state.settings)
    seconds = settings.command_idempotency_retention_seconds
    if seconds is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Command idempotency retention is not configured",
        )
    return utc_now() + timedelta(seconds=seconds)


@router.get("/riders", response_model=list[ManagerRiderResponse])
async def get_manager_riders(
    _principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    cell_id: str | None = Query(default=None, max_length=20),
) -> list[ManagerRiderResponse]:
    return [_rider_response(row) for row in await list_manager_riders(session, cell_id=cell_id)]


@router.get(
    "/collection-groups/pending-assignment",
    response_model=list[PendingGroupResponse],
)
async def get_pending_assignment_groups(
    _principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    cell_id: str | None = Query(default=None, max_length=20),
) -> list[PendingGroupResponse]:
    return [
        _pending_group_response(row)
        for row in await list_pending_assignment_groups(session, cell_id=cell_id)
    ]


@router.post(
    "/collection-groups/{collection_group_id}/assign",
    response_model=AssignmentSummaryResponse,
)
async def post_manual_assignment(
    collection_group_id: UUID,
    body: ManualAssignmentRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AssignmentSummaryResponse:
    def _not_found() -> HTTPException:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    try:
        async with session_factory() as session, session.begin():
            return _assignment_response(
                await assign_group_manually(
                    session,
                    collection_group_id=collection_group_id,
                    rider_id=body.rider_id,
                    manager_user_id=principal.user_id,
                )
            )
    except CollectionGroupNotFoundError as error:
        raise _not_found() from error
    except (
        RiderNotFoundError,
        RiderNotEligibleError,
        PickupGroupNotAssignableError,
        GroupAlreadyAssignedError,
        AssignmentStateInconsistentError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.get("/incidents", response_model=list[ManagerIncidentResponse])
async def get_manager_incidents(
    _principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[ManagerIncidentResponse]:
    return [_incident_response(row) for row in await list_open_incidents(session)]


@router.post(
    "/assignments/{predecessor_assignment_id}/reassign",
    response_model=AssignmentSummaryResponse,
)
async def post_reassignment(
    predecessor_assignment_id: UUID,
    body: ReassignmentRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AssignmentSummaryResponse:
    def _not_found() -> HTTPException:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    try:
        async with session_factory() as session, session.begin():
            return _assignment_response(
                await reassign_outstanding_work(
                    session,
                    predecessor_assignment_id=predecessor_assignment_id,
                    replacement_rider_id=body.replacement_rider_id,
                    manager_user_id=principal.user_id,
                    client_reassignment_id=body.client_reassignment_id,
                    incident_id=body.incident_id,
                    idempotency_expires_at=_idempotency_expiry(request),
                )
            )
    except (ReassignmentNotFoundError, PickupIncidentNotFoundError) as error:
        raise _not_found() from error
    except (
        IdempotencyKeyConflictError,
        ReassignmentCommandInProgressError,
        ReassignmentConflictError,
        ReassignmentStateError,
        ReassignmentRiderError,
        PickupIncidentStateError,
        PickupIncidentConflictError,
        IntegrityError,
    ) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post(
    "/collection-requests/{request_id}/complete",
    response_model=CompletionResponse,
)
async def post_complete_collection_request(
    request_id: UUID,
    _principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> CompletionResponse:
    def _not_found() -> HTTPException:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    def _conflict(error: Exception) -> HTTPException:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))

    try:
        async with session_factory() as session, session.begin():
            return _completion_response(await complete_collection_request(session, request_id))
    except CollectionRequestNotFoundError as error:
        raise _not_found() from error
    except CollectionRequestCompletionError as error:
        raise _conflict(error) from error


class CreateFleetRequest(ManagerCommandModel):
    name: str


class SetFleetStatusRequest(ManagerCommandModel):
    status: str


class ManageFleetRiderRequest(ManagerCommandModel):
    rider_id: UUID


class ManageFleetCellRequest(ManagerCommandModel):
    cell_id: str


@router.post("/fleets")
async def api_create_fleet(
    request: CreateFleetRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, str]:
    async with session_factory() as session, session.begin():
        fleet = await create_fleet(session, request.name)
        return {"fleet_id": str(fleet.fleet_id)}


@router.put("/fleets/{fleet_id}/status")
async def api_set_fleet_status(
    fleet_id: UUID,
    request: SetFleetStatusRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, str]:
    async with session_factory() as session, session.begin():
        try:
            await set_fleet_status(session, fleet_id, request.status)
        except FleetManagerError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return {"status": "ok"}


@router.post("/fleets/{fleet_id}/riders")
async def api_add_rider_to_fleet(
    fleet_id: UUID,
    request: ManageFleetRiderRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, str]:
    async with session_factory() as session, session.begin():
        try:
            membership = await add_rider_to_fleet(session, fleet_id, request.rider_id)
        except FleetManagerError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return {"fleet_membership_id": str(membership.fleet_membership_id)}


@router.delete("/riders/{rider_id}/fleet")
async def api_remove_rider_from_fleet(
    rider_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, str]:
    async with session_factory() as session, session.begin():
        await end_rider_fleet_membership(session, rider_id)
        return {"status": "ok"}


@router.post("/fleets/{fleet_id}/cells")
async def api_activate_fleet_cell(
    fleet_id: UUID,
    request: ManageFleetCellRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, str]:
    async with session_factory() as session, session.begin():
        await activate_fleet_cell(session, fleet_id, request.cell_id)
        return {"status": "ok"}


@router.delete("/fleets/{fleet_id}/cells/{cell_id}")
async def api_deactivate_fleet_cell(
    fleet_id: UUID,
    cell_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, str]:
    async with session_factory() as session, session.begin():
        await deactivate_fleet_cell(session, fleet_id, cell_id)
        return {"status": "ok"}


@router.post("/riders/{rider_id}/cells")
async def api_activate_rider_cell(
    rider_id: UUID,
    request: ManageFleetCellRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, str]:
    async with session_factory() as session, session.begin():
        await activate_rider_cell(session, rider_id, request.cell_id)
        return {"status": "ok"}


@router.delete("/riders/{rider_id}/cells/{cell_id}")
async def api_deactivate_rider_cell(
    rider_id: UUID,
    cell_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> dict[str, str]:
    async with session_factory() as session, session.begin():
        await deactivate_rider_cell(session, rider_id, cell_id)
        return {"status": "ok"}
