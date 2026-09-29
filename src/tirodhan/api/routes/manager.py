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
    profile_status: str
    vehicle_type_code: str | None
    capacity_class_code: str | None
    availability_intent: str
    work_state: str
    version: int
    updated_at: datetime


class PendingGroupResponse(BaseModel):
    collection_group_id: UUID
    planning_batch_id: UUID
    planning_mode: str
    cell_id: str
    slot_start: datetime
    slot_end: datetime
    pickup_count: int
    created_at: datetime


class ManualAssignmentRequest(ManagerCommandModel):
    rider_id: UUID


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
    client_incident_id: UUID
    pickup_execution_id: UUID
    rider_assignment_id: UUID
    reason_code: str
    status: str
    opened_at: datetime


class ReassignmentRequest(ManagerCommandModel):
    client_reassignment_id: UUID
    replacement_rider_id: UUID
    incident_id: UUID | None = None


class CompletionResponse(BaseModel):
    request_id: UUID
    status: str
    completed_at: datetime | None


def _rider_response(value: ManagerRiderRead) -> ManagerRiderResponse:
    return ManagerRiderResponse(
        rider_id=value.rider_id,
        profile_status=value.profile_status,
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
        planning_batch_id=value.planning_batch_id,
        planning_mode=value.planning_mode,
        cell_id=value.cell_id,
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
        client_incident_id=value.client_incident_id,
        pickup_execution_id=value.pickup_execution_id,
        rider_assignment_id=value.rider_assignment_id,
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
    seconds = settings.command_idempotency_ttl_seconds
    if seconds is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Command idempotency retention is not configured",
        )
    return utc_now() + timedelta(seconds=seconds)


def _conflict(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")


@router.get("/riders", response_model=list[ManagerRiderResponse])
async def get_manager_riders(
    _principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ManagerRiderResponse]:
    return [_rider_response(value) for value in await list_manager_riders(session, limit=limit)]


@router.get(
    "/collection-groups/pending-assignment",
    response_model=list[PendingGroupResponse],
)
async def get_pending_assignment_groups(
    _principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[PendingGroupResponse]:
    return [
        _pending_group_response(value)
        for value in await list_pending_assignment_groups(session, limit=limit)
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
    try:
        return _assignment_response(
            await assign_group_manually(
                session_factory,
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
        GroupAlreadyAssignedError,
        AssignmentStateInconsistentError,
        PickupGroupNotAssignableError,
        IntegrityError,
    ) as error:
        raise _conflict(error) from error


@router.get("/incidents", response_model=list[ManagerIncidentResponse])
async def get_manager_incidents(
    _principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ManagerIncidentResponse]:
    return [_incident_response(value) for value in await list_open_incidents(session, limit=limit)]


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
    try:
        return _assignment_response(
            await reassign_outstanding_work(
                session_factory,
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
        ReassignmentRiderError,
        ReassignmentStateError,
        PickupIncidentConflictError,
        PickupIncidentStateError,
        IntegrityError,
    ) as error:
        raise _conflict(error) from error


@router.post(
    "/collection-requests/{request_id}/complete",
    response_model=CompletionResponse,
)
async def post_complete_collection_request(
    request_id: UUID,
    _principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> CompletionResponse:
    try:
        return _completion_response(await complete_collection_request(session_factory, request_id))
    except CollectionRequestNotFoundError as error:
        raise _not_found() from error
    except CollectionRequestCompletionError as error:
        raise _conflict(error) from error
