from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.customers.service import command_fingerprint
from tirodhan.modules.dispatch.models import (
    RiderAssignment,
    RiderAssignmentItem,
    RiderAvailability,
    RiderProfile,
)
from tirodhan.modules.pickups.models import PickupIncident
from tirodhan.modules.planning.models import CollectionGroup, PickupExecution
from tirodhan.modules.reliability.primitives import (
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)

ASSIGNMENT_ACTIVE = "ACTIVE"
ASSIGNMENT_COMPLETED = "COMPLETED"
ASSIGNMENT_SUPERSEDED = "SUPERSEDED"
SOURCE_MANAGER_ASSIGNED = "MANAGER_ASSIGNED"
RIDER_ACTIVE = "ACTIVE"
INTENT_AVAILABLE = "AVAILABLE"
WORK_IDLE = "IDLE"
WORK_RESERVED = "RESERVED"
WORK_BUSY = "BUSY"
PICKUP_ASSIGNED = "ASSIGNED"
PICKUP_COLLECTED = "COLLECTED"
RELEASE_REASSIGNED = "REASSIGNED"
INCIDENT_OPEN = "OPEN"
INCIDENT_RESOLVED = "RESOLVED"
RESOLUTION_REASSIGNED = "REASSIGNED"
REASSIGNMENT_IDEMPOTENCY_SCOPE = "pickup-reassignment"

INCIDENT_REASON_CODES = frozenset(
    {
        "CUSTOMER_UNAVAILABLE",
        "ADDRESS_NOT_FOUND",
        "ACCESS_BLOCKED",
        "RIDER_UNABLE_TO_REACH",
        "RIDER_UNABLE_TO_CONTINUE",
        "OTHER",
    }
)


class PickupOperationsError(RuntimeError):
    pass


class PickupIncidentNotFoundError(PickupOperationsError):
    pass


class PickupIncidentConflictError(PickupOperationsError):
    pass


class PickupIncidentStateError(PickupOperationsError):
    pass


class InvalidIncidentReasonError(ValueError):
    pass


class ReassignmentNotFoundError(PickupOperationsError):
    pass


class ReassignmentConflictError(PickupOperationsError):
    pass


class ReassignmentStateError(PickupOperationsError):
    pass


class ReassignmentRiderError(PickupOperationsError):
    pass


class ReassignmentCommandInProgressError(PickupOperationsError):
    pass


def _require_incident_reason(reason_code: str) -> None:
    if reason_code not in INCIDENT_REASON_CODES:
        raise InvalidIncidentReasonError("pickup incident reason is not supported")


async def open_pickup_incident(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pickup_execution_id: UUID,
    rider_id: UUID,
    client_incident_id: UUID,
    reason_code: str,
    now: datetime | None = None,
) -> PickupIncident:
    _require_incident_reason(reason_code)
    async with session_factory() as session, session.begin():
        existing = await _incident_by_client_id(session, client_incident_id)
        if existing is not None:
            await _validate_incident_replay(
                session,
                existing,
                pickup_execution_id=pickup_execution_id,
                rider_id=rider_id,
                reason_code=reason_code,
            )
            return existing

        assignment_id = await session.scalar(
            select(RiderAssignmentItem.assignment_id).where(
                RiderAssignmentItem.pickup_execution_id == pickup_execution_id,
                RiderAssignmentItem.released_at.is_(None),
            )
        )
        if assignment_id is None:
            pickup_exists = await session.scalar(
                select(PickupExecution.pickup_execution_id).where(
                    PickupExecution.pickup_execution_id == pickup_execution_id
                )
            )
            if pickup_exists is None:
                raise PickupIncidentNotFoundError("pickup execution not found")
            raise PickupIncidentStateError("pickup has no current assignment ownership")

        assignment = await _lock_assignment(session, assignment_id)
        existing = await _incident_by_client_id(session, client_incident_id)
        if existing is not None:
            await _validate_incident_replay(
                session,
                existing,
                pickup_execution_id=pickup_execution_id,
                rider_id=rider_id,
                reason_code=reason_code,
            )
            return existing

        if assignment.rider_id != rider_id:
            raise PickupIncidentConflictError("pickup assignment belongs to another rider")
        if assignment.status != ASSIGNMENT_ACTIVE or assignment.started_at is None:
            raise PickupIncidentStateError("fresh incident requires an active started assignment")

        availability = await _lock_availability(session, rider_id)
        if availability.work_state != WORK_BUSY:
            raise PickupIncidentStateError("fresh incident requires a BUSY current rider")

        pickup = await session.scalar(
            select(PickupExecution)
            .where(PickupExecution.pickup_execution_id == pickup_execution_id)
            .with_for_update()
        )
        if pickup is None:
            raise PickupIncidentNotFoundError("pickup execution not found")
        ownership = await session.scalar(
            select(RiderAssignmentItem)
            .where(
                RiderAssignmentItem.assignment_id == assignment.assignment_id,
                RiderAssignmentItem.pickup_execution_id == pickup_execution_id,
                RiderAssignmentItem.released_at.is_(None),
            )
            .with_for_update()
        )
        if ownership is None:
            raise PickupIncidentStateError("pickup is not currently owned by the assignment")

        opened_at = now or utc_now()
        incident = PickupIncident(
            incident_id=new_uuid7(),
            client_incident_id=client_incident_id,
            pickup_execution_id=pickup_execution_id,
            rider_assignment_id=assignment.assignment_id,
            reason_code=reason_code,
            status=INCIDENT_OPEN,
            resolution_code=None,
            opened_at=opened_at,
            resolved_at=None,
            resolved_by_user_id=None,
            created_at=opened_at,
        )
        session.add(incident)
        await session.flush([incident])
        return incident


async def reassign_outstanding_work(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    predecessor_assignment_id: UUID,
    replacement_rider_id: UUID,
    manager_user_id: UUID,
    client_reassignment_id: UUID,
    idempotency_expires_at: datetime,
    incident_id: UUID | None = None,
    now: datetime | None = None,
) -> RiderAssignment:
    fingerprint = command_fingerprint(
        {
            "predecessor_assignment_id": predecessor_assignment_id,
            "replacement_rider_id": replacement_rider_id,
            "manager_user_id": manager_user_id,
            "incident_id": incident_id,
        }
    )
    async with session_factory() as session, session.begin():
        claim = await claim_idempotency_record(
            session,
            scope=REASSIGNMENT_IDEMPOTENCY_SCOPE,
            idempotency_key=str(client_reassignment_id),
            request_fingerprint=fingerprint,
            expires_at=idempotency_expires_at,
        )
        if not claim.created:
            result = get_completed_idempotency_result(claim.record)
            if result is None or result.resource_id is None:
                raise ReassignmentCommandInProgressError("reassignment command is in progress")
            successor = await session.get(RiderAssignment, result.resource_id)
            if successor is None:
                raise ReassignmentStateError(
                    "completed reassignment references a missing successor"
                )
            if successor.supersedes_assignment_id != predecessor_assignment_id:
                raise ReassignmentStateError(
                    "completed reassignment references an invalid successor"
                )
            return successor

        group_id = await session.scalar(
            select(RiderAssignment.collection_group_id).where(
                RiderAssignment.assignment_id == predecessor_assignment_id
            )
        )
        if group_id is None:
            raise ReassignmentNotFoundError("predecessor assignment not found")
        group = await _lock_group(session, group_id)
        predecessor = await _lock_assignment(session, predecessor_assignment_id)
        if predecessor.collection_group_id != group.collection_group_id:
            raise ReassignmentStateError("predecessor assignment group changed")
        if predecessor.status != ASSIGNMENT_ACTIVE:
            raise ReassignmentConflictError("predecessor assignment is no longer ACTIVE")
        if predecessor.completed_at is not None or predecessor.superseded_at is not None:
            raise ReassignmentStateError("active predecessor has terminal timestamps")
        if replacement_rider_id == predecessor.rider_id:
            raise ReassignmentRiderError("replacement rider must differ from predecessor rider")

        (
            predecessor_availability,
            replacement_profile,
            replacement_availability,
        ) = await _lock_reassignment_riders(
            session,
            predecessor_rider_id=predecessor.rider_id,
            replacement_rider_id=replacement_rider_id,
        )
        if (
            replacement_profile.status != RIDER_ACTIVE
            or replacement_availability.availability_intent != INTENT_AVAILABLE
            or replacement_availability.work_state != WORK_IDLE
        ):
            raise ReassignmentRiderError("replacement rider must be ACTIVE, AVAILABLE, and IDLE")
        expected_predecessor_work_state = (
            WORK_RESERVED if predecessor.started_at is None else WORK_BUSY
        )
        if predecessor_availability.work_state != expected_predecessor_work_state:
            raise ReassignmentStateError("predecessor rider work state is inconsistent")

        pickups, items = await _lock_predecessor_population(
            session, predecessor_assignment_id=predecessor.assignment_id
        )
        transferable: list[tuple[PickupExecution, RiderAssignmentItem]] = []
        for pickup, item in zip(pickups, items, strict=True):
            if pickup.pickup_execution_id != item.pickup_execution_id:
                raise ReassignmentStateError("locked reassignment population is inconsistent")
            if pickup.status == PICKUP_COLLECTED:
                continue
            if pickup.status == PICKUP_ASSIGNED:
                transferable.append((pickup, item))
                continue
            raise ReassignmentStateError(
                f"unreleased pickup has unsupported status {pickup.status}"
            )
        if not transferable:
            raise ReassignmentConflictError("predecessor has no transferable ASSIGNED pickup")

        incident: PickupIncident | None = None
        if incident_id is not None:
            incident = await session.scalar(
                select(PickupIncident)
                .where(PickupIncident.incident_id == incident_id)
                .with_for_update()
            )
            if incident is None:
                raise PickupIncidentNotFoundError("pickup incident not found")
            if (
                incident.rider_assignment_id != predecessor.assignment_id
                or incident.pickup_execution_id
                not in {pickup.pickup_execution_id for pickup in pickups}
            ):
                raise PickupIncidentConflictError(
                    "pickup incident does not belong to predecessor population"
                )
            if incident.status != INCIDENT_OPEN:
                raise PickupIncidentStateError("pickup incident is not OPEN")

        reassigned_at = now or utc_now()
        predecessor.status = ASSIGNMENT_SUPERSEDED
        predecessor.superseded_at = reassigned_at
        for _pickup, item in transferable:
            item.released_at = reassigned_at
            item.release_reason_code = RELEASE_REASSIGNED
        await session.flush()

        successor = RiderAssignment(
            assignment_id=new_uuid7(),
            collection_group_id=predecessor.collection_group_id,
            rider_id=replacement_rider_id,
            source=SOURCE_MANAGER_ASSIGNED,
            status=ASSIGNMENT_ACTIVE,
            assigned_by_user_id=manager_user_id,
            supersedes_assignment_id=predecessor.assignment_id,
            created_at=reassigned_at,
            assigned_at=reassigned_at,
            started_at=None,
            completed_at=None,
            superseded_at=None,
        )
        successor_items = [
            RiderAssignmentItem(
                assignment_id=successor.assignment_id,
                pickup_execution_id=pickup.pickup_execution_id,
                assigned_at=reassigned_at,
                released_at=None,
                release_reason_code=None,
            )
            for pickup, _item in transferable
        ]
        session.add(successor)
        session.add_all(successor_items)
        await session.flush([successor, *successor_items])

        predecessor_result = await session.execute(
            update(RiderAvailability)
            .where(
                RiderAvailability.rider_id == predecessor.rider_id,
                RiderAvailability.work_state == expected_predecessor_work_state,
                RiderAvailability.version == predecessor_availability.version,
            )
            .values(
                work_state=WORK_IDLE,
                version=RiderAvailability.version + 1,
                updated_at=reassigned_at,
            )
        )
        replacement_result = await session.execute(
            update(RiderAvailability)
            .where(
                RiderAvailability.rider_id == replacement_rider_id,
                RiderAvailability.availability_intent == INTENT_AVAILABLE,
                RiderAvailability.work_state == WORK_IDLE,
                RiderAvailability.version == replacement_availability.version,
            )
            .values(
                work_state=WORK_RESERVED,
                version=RiderAvailability.version + 1,
                updated_at=reassigned_at,
            )
        )
        if cast(CursorResult[Any], predecessor_result).rowcount != 1:
            raise ReassignmentStateError("predecessor rider work state changed")
        if cast(CursorResult[Any], replacement_result).rowcount != 1:
            raise ReassignmentRiderError("replacement rider eligibility changed")

        if incident is not None:
            incident.status = INCIDENT_RESOLVED
            incident.resolution_code = RESOLUTION_REASSIGNED
            incident.resolved_at = reassigned_at
            incident.resolved_by_user_id = manager_user_id

        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=successor.assignment_id,
            result_status_code=None,
            completed_at=reassigned_at,
        )
        return successor


async def _incident_by_client_id(
    session: AsyncSession, client_incident_id: UUID
) -> PickupIncident | None:
    return cast(
        PickupIncident | None,
        await session.scalar(
            select(PickupIncident).where(PickupIncident.client_incident_id == client_incident_id)
        ),
    )


async def _validate_incident_replay(
    session: AsyncSession,
    incident: PickupIncident,
    *,
    pickup_execution_id: UUID,
    rider_id: UUID,
    reason_code: str,
) -> None:
    assignment_rider_id = await session.scalar(
        select(RiderAssignment.rider_id)
        .join(
            RiderAssignmentItem,
            RiderAssignmentItem.assignment_id == RiderAssignment.assignment_id,
        )
        .where(
            RiderAssignment.assignment_id == incident.rider_assignment_id,
            RiderAssignmentItem.pickup_execution_id == incident.pickup_execution_id,
        )
    )
    if (
        incident.pickup_execution_id != pickup_execution_id
        or incident.reason_code != reason_code
        or assignment_rider_id != rider_id
    ):
        raise PickupIncidentConflictError(
            "client incident ID is associated with different incident facts"
        )


async def _lock_group(session: AsyncSession, group_id: UUID) -> CollectionGroup:
    group = await session.scalar(
        select(CollectionGroup)
        .where(CollectionGroup.collection_group_id == group_id)
        .with_for_update()
    )
    if group is None:
        raise ReassignmentNotFoundError("collection group not found")
    return group


async def _lock_assignment(session: AsyncSession, assignment_id: UUID) -> RiderAssignment:
    assignment = await session.scalar(
        select(RiderAssignment)
        .where(RiderAssignment.assignment_id == assignment_id)
        .with_for_update()
    )
    if assignment is None:
        raise ReassignmentNotFoundError("rider assignment not found")
    return assignment


async def _lock_availability(session: AsyncSession, rider_id: UUID) -> RiderAvailability:
    availability = await session.scalar(
        select(RiderAvailability).where(RiderAvailability.rider_id == rider_id).with_for_update()
    )
    if availability is None:
        raise ReassignmentStateError("rider availability is missing")
    return availability


async def _lock_reassignment_riders(
    session: AsyncSession,
    *,
    predecessor_rider_id: UUID,
    replacement_rider_id: UUID,
) -> tuple[RiderAvailability, RiderProfile, RiderAvailability]:
    predecessor_availability: RiderAvailability | None = None
    replacement_profile: RiderProfile | None = None
    replacement_availability: RiderAvailability | None = None
    for rider_id in sorted(
        (predecessor_rider_id, replacement_rider_id), key=lambda value: value.int
    ):
        if rider_id == replacement_rider_id:
            replacement_profile = await session.scalar(
                select(RiderProfile).where(RiderProfile.rider_id == rider_id).with_for_update()
            )
            if replacement_profile is None:
                raise ReassignmentRiderError("replacement rider profile not found")
            replacement_availability = await _lock_availability(session, rider_id)
        else:
            predecessor_availability = await _lock_availability(session, rider_id)
    if (
        predecessor_availability is None
        or replacement_profile is None
        or replacement_availability is None
    ):
        raise ReassignmentStateError("reassignment rider locks are incomplete")
    return predecessor_availability, replacement_profile, replacement_availability


async def _lock_predecessor_population(
    session: AsyncSession, *, predecessor_assignment_id: UUID
) -> tuple[list[PickupExecution], list[RiderAssignmentItem]]:
    pickup_ids = list(
        await session.scalars(
            select(RiderAssignmentItem.pickup_execution_id)
            .where(
                RiderAssignmentItem.assignment_id == predecessor_assignment_id,
                RiderAssignmentItem.released_at.is_(None),
            )
            .order_by(RiderAssignmentItem.pickup_execution_id)
        )
    )
    if not pickup_ids:
        raise ReassignmentStateError("predecessor has no unreleased assignment population")
    pickups = list(
        await session.scalars(
            select(PickupExecution)
            .where(PickupExecution.pickup_execution_id.in_(pickup_ids))
            .order_by(PickupExecution.pickup_execution_id)
            .with_for_update()
        )
    )
    items = list(
        await session.scalars(
            select(RiderAssignmentItem)
            .where(
                RiderAssignmentItem.assignment_id == predecessor_assignment_id,
                RiderAssignmentItem.pickup_execution_id.in_(pickup_ids),
                RiderAssignmentItem.released_at.is_(None),
            )
            .order_by(RiderAssignmentItem.pickup_execution_id)
            .with_for_update()
        )
    )
    if len(pickups) != len(pickup_ids) or len(items) != len(pickup_ids):
        raise ReassignmentStateError("predecessor assignment population changed")
    return pickups, items
