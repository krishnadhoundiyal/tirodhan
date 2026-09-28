from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch.models import (
    RiderAssignment,
    RiderAssignmentItem,
    RiderAvailability,
    RiderProfile,
)
from tirodhan.modules.pickups.models import PickupAttempt
from tirodhan.modules.planning.models import PickupExecution

ASSIGNMENT_ACTIVE = "ACTIVE"
ASSIGNMENT_COMPLETED = "COMPLETED"
RIDER_ACTIVE = "ACTIVE"
WORK_RESERVED = "RESERVED"
WORK_BUSY = "BUSY"
WORK_IDLE = "IDLE"
PICKUP_ASSIGNED = "ASSIGNED"
PICKUP_COLLECTED = "COLLECTED"
ATTEMPT_COLLECTED = "COLLECTED"
ATTEMPT_NOT_COLLECTED = "NOT_COLLECTED"


class PickupLifecycleError(RuntimeError):
    pass


class AssignmentNotFoundError(PickupLifecycleError):
    pass


class AssignmentWrongRiderError(PickupLifecycleError):
    pass


class AssignmentNotStartableError(PickupLifecycleError):
    pass


class AssignmentStateInconsistentError(PickupLifecycleError):
    pass


class PickupExecutionNotFoundError(PickupLifecycleError):
    pass


class PickupNotAttemptableError(PickupLifecycleError):
    pass


class PickupOwnershipMismatchError(PickupLifecycleError):
    pass


class RiderWorkStateInvalidError(PickupLifecycleError):
    pass


class PickupAttemptReplayConflictError(PickupLifecycleError):
    pass


class InvalidPickupOutcomeError(ValueError):
    pass


async def start_assignment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assignment_id: UUID,
    rider_id: UUID,
    now: datetime | None = None,
) -> RiderAssignment:
    async with session_factory() as session, session.begin():
        assignment = await _lock_assignment(session, assignment_id)
        if assignment.rider_id != rider_id:
            raise AssignmentWrongRiderError("assignment belongs to another rider")

        if assignment.status == ASSIGNMENT_COMPLETED:
            if assignment.started_at is None or assignment.completed_at is None:
                raise AssignmentStateInconsistentError(
                    "completed assignment is missing lifecycle timestamps"
                )
            return assignment
        if assignment.status != ASSIGNMENT_ACTIVE:
            raise AssignmentNotStartableError("assignment is not ACTIVE")
        if assignment.completed_at is not None:
            raise AssignmentStateInconsistentError(
                "active assignment already has a completion timestamp"
            )

        profile = await _lock_rider_profile(session, rider_id)
        availability = await _lock_rider_availability(session, rider_id)
        if assignment.started_at is not None:
            if availability.work_state != WORK_BUSY:
                raise AssignmentStateInconsistentError(
                    "started active assignment requires BUSY rider work state"
                )
            return assignment

        if profile.status != RIDER_ACTIVE:
            raise AssignmentNotStartableError("rider must be ACTIVE to start new work")
        if availability.work_state != WORK_RESERVED:
            raise RiderWorkStateInvalidError("unstarted assignment requires RESERVED rider")

        started_at = now or utc_now()
        assignment.started_at = started_at
        availability_result = await session.execute(
            update(RiderAvailability)
            .where(
                RiderAvailability.rider_id == rider_id,
                RiderAvailability.work_state == WORK_RESERVED,
                RiderAvailability.version == availability.version,
            )
            .values(
                work_state=WORK_BUSY,
                version=RiderAvailability.version + 1,
                updated_at=started_at,
            )
        )
        if cast(CursorResult[Any], availability_result).rowcount != 1:
            raise RiderWorkStateInvalidError("rider work state changed during assignment start")
        return assignment


async def record_pickup_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pickup_execution_id: UUID,
    rider_id: UUID,
    client_attempt_id: UUID,
    outcome: str,
    now: datetime | None = None,
) -> PickupAttempt:
    _require_attempt_outcome(outcome)
    assignment_id = await _discover_assignment_id(
        session_factory,
        pickup_execution_id=pickup_execution_id,
        client_attempt_id=client_attempt_id,
    )

    async with session_factory() as session, session.begin():
        assignment = await _lock_assignment(session, assignment_id)
        existing = await session.scalar(
            select(PickupAttempt).where(
                PickupAttempt.pickup_execution_id == pickup_execution_id,
                PickupAttempt.client_attempt_id == client_attempt_id,
            )
        )
        if existing is not None:
            if assignment.rider_id != rider_id:
                raise AssignmentWrongRiderError("historical attempt belongs to another rider")
            if existing.rider_assignment_id != assignment.assignment_id:
                raise AssignmentStateInconsistentError(
                    "pickup attempt historical assignment does not match"
                )
            if existing.outcome != outcome:
                raise PickupAttemptReplayConflictError(
                    "client attempt ID is associated with another outcome"
                )
            return existing

        if assignment.rider_id != rider_id:
            raise AssignmentWrongRiderError("assignment belongs to another rider")
        if (
            assignment.status != ASSIGNMENT_ACTIVE
            or assignment.started_at is None
            or assignment.completed_at is not None
        ):
            raise AssignmentNotStartableError("assignment is not active and started")

        availability = await _lock_rider_availability(session, rider_id)
        if availability.work_state != WORK_BUSY:
            raise RiderWorkStateInvalidError("fresh pickup attempt requires BUSY rider")

        pickup = await session.scalar(
            select(PickupExecution)
            .where(PickupExecution.pickup_execution_id == pickup_execution_id)
            .with_for_update()
        )
        if pickup is None:
            raise PickupExecutionNotFoundError("pickup execution not found")
        if pickup.status != PICKUP_ASSIGNED:
            raise PickupNotAttemptableError("pickup execution is no longer ASSIGNED")

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
            raise PickupOwnershipMismatchError(
                "pickup is not currently owned by the locked assignment"
            )

        attempt_number = int(
            await session.scalar(
                select(func.coalesce(func.max(PickupAttempt.attempt_number), 0) + 1).where(
                    PickupAttempt.pickup_execution_id == pickup_execution_id
                )
            )
            or 1
        )
        attempted_at = now or utc_now()
        attempt = PickupAttempt(
            pickup_attempt_id=new_uuid7(),
            pickup_execution_id=pickup_execution_id,
            rider_assignment_id=assignment.assignment_id,
            client_attempt_id=client_attempt_id,
            attempt_number=attempt_number,
            outcome=outcome,
            attempted_at=attempted_at,
            created_at=attempted_at,
        )
        session.add(attempt)
        await session.flush([attempt])
        if outcome == ATTEMPT_NOT_COLLECTED:
            return attempt

        pickup_result = await session.execute(
            update(PickupExecution)
            .where(
                PickupExecution.pickup_execution_id == pickup_execution_id,
                PickupExecution.status == PICKUP_ASSIGNED,
                PickupExecution.collected_at.is_(None),
            )
            .values(
                status=PICKUP_COLLECTED,
                collected_at=attempted_at,
                updated_at=attempted_at,
            )
        )
        if cast(CursorResult[Any], pickup_result).rowcount != 1:
            raise PickupNotAttemptableError("pickup state changed during collection")
        await _complete_assignment_if_finished(
            session,
            assignment=assignment,
            availability=availability,
            completed_at=attempted_at,
        )
        return attempt


async def _discover_assignment_id(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pickup_execution_id: UUID,
    client_attempt_id: UUID,
) -> UUID:
    async with session_factory() as session:
        historical = await session.scalar(
            select(PickupAttempt.rider_assignment_id).where(
                PickupAttempt.pickup_execution_id == pickup_execution_id,
                PickupAttempt.client_attempt_id == client_attempt_id,
            )
        )
        if historical is not None:
            return historical
        current = await session.scalar(
            select(RiderAssignmentItem.assignment_id).where(
                RiderAssignmentItem.pickup_execution_id == pickup_execution_id,
                RiderAssignmentItem.released_at.is_(None),
            )
        )
        if current is not None:
            return current
        pickup_exists = await session.scalar(
            select(PickupExecution.pickup_execution_id).where(
                PickupExecution.pickup_execution_id == pickup_execution_id
            )
        )
        if pickup_exists is None:
            raise PickupExecutionNotFoundError("pickup execution not found")
        raise PickupOwnershipMismatchError("pickup has no current assignment ownership")


def _require_attempt_outcome(outcome: str) -> None:
    if outcome not in {ATTEMPT_COLLECTED, ATTEMPT_NOT_COLLECTED}:
        raise InvalidPickupOutcomeError("pickup attempt outcome is not supported")


async def _complete_assignment_if_finished(
    session: AsyncSession,
    *,
    assignment: RiderAssignment,
    availability: RiderAvailability,
    completed_at: datetime,
) -> bool:
    remaining = await session.scalar(
        select(func.count())
        .select_from(RiderAssignmentItem)
        .join(
            PickupExecution,
            PickupExecution.pickup_execution_id == RiderAssignmentItem.pickup_execution_id,
        )
        .where(
            RiderAssignmentItem.assignment_id == assignment.assignment_id,
            RiderAssignmentItem.released_at.is_(None),
            PickupExecution.status != PICKUP_COLLECTED,
        )
    )
    if remaining:
        return False

    assignment_result = await session.execute(
        update(RiderAssignment)
        .where(
            RiderAssignment.assignment_id == assignment.assignment_id,
            RiderAssignment.status == ASSIGNMENT_ACTIVE,
            RiderAssignment.completed_at.is_(None),
        )
        .values(status=ASSIGNMENT_COMPLETED, completed_at=completed_at)
    )
    if cast(CursorResult[Any], assignment_result).rowcount != 1:
        raise AssignmentStateInconsistentError("assignment completion state changed")
    availability_result = await session.execute(
        update(RiderAvailability)
        .where(
            RiderAvailability.rider_id == assignment.rider_id,
            RiderAvailability.work_state == WORK_BUSY,
            RiderAvailability.version == availability.version,
        )
        .values(
            work_state=WORK_IDLE,
            version=RiderAvailability.version + 1,
            updated_at=completed_at,
        )
    )
    if cast(CursorResult[Any], availability_result).rowcount != 1:
        raise RiderWorkStateInvalidError("rider work state changed during assignment completion")
    return True


async def _lock_assignment(session: AsyncSession, assignment_id: UUID) -> RiderAssignment:
    assignment = await session.scalar(
        select(RiderAssignment)
        .where(RiderAssignment.assignment_id == assignment_id)
        .with_for_update()
    )
    if assignment is None:
        raise AssignmentNotFoundError("rider assignment not found")
    return assignment


async def _lock_rider_profile(session: AsyncSession, rider_id: UUID) -> RiderProfile:
    profile = await session.scalar(
        select(RiderProfile).where(RiderProfile.rider_id == rider_id).with_for_update()
    )
    if profile is None:
        raise AssignmentStateInconsistentError("rider profile is missing")
    return profile


async def _lock_rider_availability(session: AsyncSession, rider_id: UUID) -> RiderAvailability:
    availability = await session.scalar(
        select(RiderAvailability).where(RiderAvailability.rider_id == rider_id).with_for_update()
    )
    if availability is None:
        raise AssignmentStateInconsistentError("rider availability is missing")
    return availability
