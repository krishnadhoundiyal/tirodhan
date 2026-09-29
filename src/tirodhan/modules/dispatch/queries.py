from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.collection_requests.models import (
    CollectionRequest,
    CollectionRequestItem,
)
from tirodhan.modules.dispatch.models import (
    AssignmentOffer,
    RiderAssignment,
    RiderAssignmentItem,
    RiderAvailability,
    RiderProfile,
)
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.identity.service import APP_USER_ACTIVE, ROLE_RIDER
from tirodhan.modules.planning.models import CollectionGroup, PickupExecution, PlanningBatch


class OperationalReadStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeclaredItemRead:
    item_category_code: str
    declared_quantity: int | None
    declared_weight_grams: int | None


@dataclass(frozen=True, slots=True)
class AssignmentPickupRead:
    pickup_execution_id: UUID
    request_id: UUID
    status: str
    slot_start: datetime
    slot_end: datetime
    pickup_address_snapshot_encrypted: bytes
    pickup_location: Any
    items: tuple[DeclaredItemRead, ...]


@dataclass(frozen=True, slots=True)
class ActiveAssignmentRead:
    assignment: RiderAssignment
    pickups: tuple[AssignmentPickupRead, ...]


@dataclass(frozen=True, slots=True)
class ManagerRiderRead:
    rider_id: UUID
    profile_status: str
    vehicle_type_code: str | None
    capacity_class_code: str | None
    availability_intent: str
    work_state: str
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PendingGroupRead:
    collection_group_id: UUID
    planning_batch_id: UUID
    planning_mode: str
    cell_id: str
    slot_start: datetime
    slot_end: datetime
    pickup_count: int
    created_at: datetime


async def get_rider_availability(
    session: AsyncSession, *, rider_id: UUID
) -> RiderAvailability | None:
    return await session.get(RiderAvailability, rider_id)


async def list_actionable_offers(
    session: AsyncSession,
    *,
    rider_id: UUID,
    now: datetime,
    limit: int = 100,
) -> list[AssignmentOffer]:
    return list(
        await session.scalars(
            select(AssignmentOffer)
            .where(
                AssignmentOffer.rider_id == rider_id,
                AssignmentOffer.status == "OPEN",
                AssignmentOffer.expires_at > now,
            )
            .order_by(AssignmentOffer.offered_at, AssignmentOffer.offer_id)
            .limit(limit)
        )
    )


async def get_active_assignment(
    session: AsyncSession,
    *,
    rider_id: UUID,
) -> ActiveAssignmentRead | None:
    assignments = list(
        await session.scalars(
            select(RiderAssignment)
            .where(
                RiderAssignment.rider_id == rider_id,
                RiderAssignment.status == "ACTIVE",
            )
            .order_by(RiderAssignment.assigned_at, RiderAssignment.assignment_id)
            .limit(2)
        )
    )
    if not assignments:
        return None
    if len(assignments) != 1:
        raise OperationalReadStateError("rider has more than one active assignment")
    assignment = assignments[0]
    pickup_rows = (
        await session.execute(
            select(PickupExecution, CollectionRequest)
            .join(
                RiderAssignmentItem,
                RiderAssignmentItem.pickup_execution_id == PickupExecution.pickup_execution_id,
            )
            .join(
                CollectionRequest,
                CollectionRequest.request_id == PickupExecution.request_id,
            )
            .where(
                RiderAssignmentItem.assignment_id == assignment.assignment_id,
                RiderAssignmentItem.released_at.is_(None),
            )
            .order_by(PickupExecution.pickup_execution_id)
        )
    ).all()
    request_ids = [request.request_id for _pickup, request in pickup_rows]
    items_by_request: dict[UUID, list[DeclaredItemRead]] = {
        request_id: [] for request_id in request_ids
    }
    if request_ids:
        items = await session.scalars(
            select(CollectionRequestItem)
            .where(CollectionRequestItem.request_id.in_(request_ids))
            .order_by(
                CollectionRequestItem.request_id,
                CollectionRequestItem.created_at,
                CollectionRequestItem.request_item_id,
            )
        )
        for item in items:
            items_by_request[item.request_id].append(
                DeclaredItemRead(
                    item_category_code=item.item_category_code,
                    declared_quantity=item.declared_quantity,
                    declared_weight_grams=item.declared_weight_grams,
                )
            )
    pickups = tuple(
        AssignmentPickupRead(
            pickup_execution_id=pickup.pickup_execution_id,
            request_id=request.request_id,
            status=pickup.status,
            slot_start=request.slot_start,
            slot_end=request.slot_end,
            pickup_address_snapshot_encrypted=bytes(request.pickup_address_snapshot_encrypted),
            pickup_location=request.pickup_location,
            items=tuple(items_by_request[request.request_id]),
        )
        for pickup, request in pickup_rows
    )
    return ActiveAssignmentRead(assignment=assignment, pickups=pickups)


async def list_manager_riders(
    session: AsyncSession,
    *,
    limit: int,
) -> list[ManagerRiderRead]:
    rows = (
        await session.execute(
            select(RiderProfile, RiderAvailability)
            .join(AppUser, AppUser.user_id == RiderProfile.rider_id)
            .join(
                UserRole,
                (UserRole.user_id == RiderProfile.rider_id)
                & (UserRole.role_code == ROLE_RIDER)
                & UserRole.revoked_at.is_(None),
            )
            .join(
                RiderAvailability,
                RiderAvailability.rider_id == RiderProfile.rider_id,
            )
            .where(AppUser.status == APP_USER_ACTIVE)
            .order_by(RiderProfile.rider_id)
            .limit(limit)
        )
    ).all()
    return [
        ManagerRiderRead(
            rider_id=profile.rider_id,
            profile_status=profile.status,
            vehicle_type_code=profile.vehicle_type_code,
            capacity_class_code=profile.capacity_class_code,
            availability_intent=availability.availability_intent,
            work_state=availability.work_state,
            version=availability.version,
            updated_at=availability.updated_at,
        )
        for profile, availability in rows
    ]


async def list_pending_assignment_groups(
    session: AsyncSession,
    *,
    limit: int,
) -> list[PendingGroupRead]:
    active_assignment_exists = exists().where(
        RiderAssignment.collection_group_id == CollectionGroup.collection_group_id,
        RiderAssignment.status == "ACTIVE",
    )
    pickup_count = func.count(PickupExecution.pickup_execution_id)
    rows = (
        await session.execute(
            select(
                CollectionGroup.collection_group_id,
                CollectionGroup.planning_batch_id,
                CollectionGroup.planning_mode,
                PlanningBatch.cell_id,
                PlanningBatch.slot_start,
                PlanningBatch.slot_end,
                pickup_count.label("pickup_count"),
                CollectionGroup.created_at,
            )
            .join(
                PlanningBatch,
                PlanningBatch.planning_batch_id == CollectionGroup.planning_batch_id,
            )
            .join(
                PickupExecution,
                PickupExecution.collection_group_id == CollectionGroup.collection_group_id,
            )
            .where(~active_assignment_exists)
            .group_by(
                CollectionGroup.collection_group_id,
                PlanningBatch.planning_batch_id,
            )
            .having(func.bool_and(PickupExecution.status == "PENDING_ASSIGNMENT"))
            .order_by(PlanningBatch.slot_start, CollectionGroup.collection_group_id)
            .limit(limit)
        )
    ).all()
    return [
        PendingGroupRead(
            collection_group_id=row.collection_group_id,
            planning_batch_id=row.planning_batch_id,
            planning_mode=row.planning_mode,
            cell_id=row.cell_id,
            slot_start=row.slot_start,
            slot_end=row.slot_end,
            pickup_count=int(row.pickup_count),
            created_at=row.created_at,
        )
        for row in rows
    ]
