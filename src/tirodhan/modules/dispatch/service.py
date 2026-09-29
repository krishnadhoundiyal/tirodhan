from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch.models import (
    AssignmentOffer,
    RiderAssignment,
    RiderAssignmentItem,
    RiderAvailability,
    RiderProfile,
)
from tirodhan.modules.identity.authorization import lock_active_user_role
from tirodhan.modules.identity.service import ROLE_RIDER
from tirodhan.modules.planning.models import CollectionGroup, PickupExecution
from tirodhan.modules.reliability.primitives import append_outbox_event

RIDER_ACTIVE = "ACTIVE"
RIDER_SUSPENDED = "SUSPENDED"
INTENT_OFFLINE = "OFFLINE"
INTENT_AVAILABLE = "AVAILABLE"
WORK_IDLE = "IDLE"
WORK_RESERVED = "RESERVED"
WORK_BUSY = "BUSY"
OFFER_OPEN = "OPEN"
OFFER_ACCEPTED = "ACCEPTED"
OFFER_CLOSED_LOST = "CLOSED_LOST"
ASSIGNMENT_ACTIVE = "ACTIVE"
SOURCE_RIDER_OFFER_ACCEPTED = "RIDER_OFFER_ACCEPTED"
SOURCE_MANAGER_ASSIGNED = "MANAGER_ASSIGNED"
PICKUP_PENDING_ASSIGNMENT = "PENDING_ASSIGNMENT"
PICKUP_ASSIGNED = "ASSIGNED"
RIDER_ASSIGNMENT_CREATED = "RiderAssignmentCreated"


class DispatchError(RuntimeError):
    pass


class CollectionGroupNotFoundError(DispatchError):
    pass


class RiderNotFoundError(DispatchError):
    pass


class RiderNotEligibleError(DispatchError):
    pass


class RiderAvailabilityVersionConflictError(DispatchError):
    pass


class AssignmentOfferNotFoundError(DispatchError):
    pass


class AssignmentOfferConflictError(DispatchError):
    pass


class AssignmentOfferExpiredError(DispatchError):
    pass


class GroupAlreadyAssignedError(DispatchError):
    pass


class AssignmentStateInconsistentError(DispatchError):
    pass


class PickupGroupNotAssignableError(DispatchError):
    pass


def _require_intent(intent: str) -> None:
    if intent not in {INTENT_OFFLINE, INTENT_AVAILABLE}:
        raise ValueError("availability intent must be OFFLINE or AVAILABLE")


def _require_offer_window(offer_round: int, expires_at: datetime, now: datetime) -> None:
    if offer_round <= 0:
        raise ValueError("offer round must be positive")
    if expires_at <= now:
        raise ValueError("offer expiry must be in the future")


async def set_rider_availability_intent(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    rider_id: UUID,
    intent: str,
    expected_version: int,
    now: datetime | None = None,
) -> RiderAvailability:
    _require_intent(intent)
    changed_at = now or utc_now()
    async with session_factory() as session, session.begin():
        availability = await session.scalar(
            update(RiderAvailability)
            .where(
                RiderAvailability.rider_id == rider_id,
                RiderAvailability.version == expected_version,
            )
            .values(
                availability_intent=intent,
                version=RiderAvailability.version + 1,
                updated_at=changed_at,
            )
            .returning(RiderAvailability)
        )
        if availability is not None:
            return availability
        exists = await session.scalar(
            select(RiderAvailability.rider_id).where(RiderAvailability.rider_id == rider_id)
        )
        if exists is None:
            raise RiderNotFoundError("rider availability not found")
        raise RiderAvailabilityVersionConflictError("rider availability version is stale")


async def create_assignment_offer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    collection_group_id: UUID,
    rider_id: UUID,
    offer_round: int,
    expires_at: datetime,
    now: datetime | None = None,
) -> AssignmentOffer:
    if offer_round <= 0:
        raise ValueError("offer round must be positive")
    async with session_factory() as session, session.begin():
        await _lock_group(session, collection_group_id)
        existing = await session.scalar(
            select(AssignmentOffer)
            .where(
                AssignmentOffer.collection_group_id == collection_group_id,
                AssignmentOffer.rider_id == rider_id,
                AssignmentOffer.offer_round == offer_round,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.expires_at != expires_at:
                raise AssignmentOfferConflictError(
                    "offer business key already has a different expiry"
                )
            return existing
        if await _active_assignment(session, collection_group_id) is not None:
            raise GroupAlreadyAssignedError("collection group already has an active assignment")
        await _require_initial_dispatch_population(session, collection_group_id)
        offered_at = now or utc_now()
        _require_offer_window(offer_round, expires_at, offered_at)
        await _lock_eligible_rider(session, rider_id)
        offer = AssignmentOffer(
            offer_id=new_uuid7(),
            collection_group_id=collection_group_id,
            rider_id=rider_id,
            offer_round=offer_round,
            status=OFFER_OPEN,
            offered_at=offered_at,
            expires_at=expires_at,
            responded_at=None,
        )
        session.add(offer)
        await session.flush([offer])
        return offer


async def accept_assignment_offer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    offer_id: UUID,
    rider_id: UUID,
    now: datetime | None = None,
) -> RiderAssignment:
    async with session_factory() as preliminary:
        identifiers = (
            await preliminary.execute(
                select(AssignmentOffer.collection_group_id, AssignmentOffer.rider_id).where(
                    AssignmentOffer.offer_id == offer_id
                )
            )
        ).one_or_none()
    if identifiers is None:
        raise AssignmentOfferNotFoundError("assignment offer not found")
    if identifiers.rider_id != rider_id:
        raise AssignmentOfferConflictError("assignment offer belongs to another rider")

    async with session_factory() as session, session.begin():
        return await _assign_group_to_rider(
            session,
            collection_group_id=identifiers.collection_group_id,
            rider_id=rider_id,
            source=SOURCE_RIDER_OFFER_ACCEPTED,
            assigned_by_user_id=None,
            offer_id=offer_id,
            now=now,
        )


async def assign_group_manually(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    collection_group_id: UUID,
    rider_id: UUID,
    manager_user_id: UUID,
    now: datetime | None = None,
) -> RiderAssignment:
    async with session_factory() as session, session.begin():
        return await _assign_group_to_rider(
            session,
            collection_group_id=collection_group_id,
            rider_id=rider_id,
            source=SOURCE_MANAGER_ASSIGNED,
            assigned_by_user_id=manager_user_id,
            offer_id=None,
            now=now,
        )


async def _assign_group_to_rider(
    session: AsyncSession,
    *,
    collection_group_id: UUID,
    rider_id: UUID,
    source: str,
    assigned_by_user_id: UUID | None,
    offer_id: UUID | None,
    now: datetime | None,
) -> RiderAssignment:
    await _lock_group(session, collection_group_id)
    established = await _active_assignment(session, collection_group_id, lock=True)
    if offer_id is None:
        if established is not None and established.rider_id != rider_id:
            raise GroupAlreadyAssignedError("collection group is assigned to another rider")
        if established is not None:
            return established

    offer: AssignmentOffer | None = None
    if offer_id is not None:
        offer = await session.scalar(
            select(AssignmentOffer).where(AssignmentOffer.offer_id == offer_id).with_for_update()
        )
        if offer is None:
            raise AssignmentOfferNotFoundError("assignment offer not found")
        if offer.collection_group_id != collection_group_id or offer.rider_id != rider_id:
            raise AssignmentOfferConflictError("assignment offer identity changed")
        if offer.status == OFFER_OPEN and offer.resolved_assignment_id is not None:
            raise AssignmentStateInconsistentError(
                "open assignment offer has a resolving assignment"
            )
        if offer.resolved_assignment_id is not None:
            resolved = await _lock_assignment(session, offer.resolved_assignment_id)
            if resolved.collection_group_id != offer.collection_group_id:
                raise AssignmentStateInconsistentError(
                    "offer resolving assignment belongs to another group"
                )
            if offer.status == OFFER_ACCEPTED:
                if resolved.rider_id != offer.rider_id:
                    raise AssignmentStateInconsistentError(
                        "accepted offer resolving assignment belongs to another rider"
                    )
                return resolved
            if offer.status == OFFER_CLOSED_LOST:
                if resolved.rider_id == offer.rider_id:
                    return resolved
                raise GroupAlreadyAssignedError("assignment offer lost to another rider")
            raise AssignmentStateInconsistentError(
                "assignment offer resolution link has unsupported status"
            )
        if established is not None:
            if established.rider_id != rider_id:
                raise GroupAlreadyAssignedError("collection group is assigned to another rider")
            raise AssignmentStateInconsistentError(
                "open assignment offer exists beside an active same-rider assignment"
            )

    profile, availability = await _lock_rider(session, rider_id)
    assignment_time = now or utc_now()
    _require_rider_eligible(profile, availability)
    await _require_fresh_rider_authorization(session, rider_id)
    if offer is not None:
        if offer.status == OFFER_CLOSED_LOST:
            raise GroupAlreadyAssignedError("assignment offer lost to another assignment")
        if offer.status != OFFER_OPEN:
            raise AssignmentStateInconsistentError("assignment offer is not open")
        if assignment_time >= offer.expires_at:
            raise AssignmentOfferExpiredError("assignment offer has expired")

    pickups = list(
        (
            await session.scalars(
                select(PickupExecution)
                .where(PickupExecution.collection_group_id == collection_group_id)
                .order_by(PickupExecution.pickup_execution_id)
                .with_for_update()
            )
        ).all()
    )
    if not pickups or any(pickup.status != PICKUP_PENDING_ASSIGNMENT for pickup in pickups):
        raise PickupGroupNotAssignableError(
            "collection group must have only PENDING_ASSIGNMENT pickups"
        )
    pickup_ids = [pickup.pickup_execution_id for pickup in pickups]
    owned = await session.scalar(
        select(RiderAssignmentItem.pickup_execution_id)
        .where(
            RiderAssignmentItem.pickup_execution_id.in_(pickup_ids),
            RiderAssignmentItem.released_at.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    if owned is not None:
        raise PickupGroupNotAssignableError("a pickup already has active assignment ownership")

    assignment = RiderAssignment(
        assignment_id=new_uuid7(),
        collection_group_id=collection_group_id,
        rider_id=rider_id,
        source=source,
        status=ASSIGNMENT_ACTIVE,
        assigned_by_user_id=assigned_by_user_id,
        supersedes_assignment_id=None,
        created_at=assignment_time,
        assigned_at=assignment_time,
        started_at=None,
        completed_at=None,
        superseded_at=None,
    )
    session.add(assignment)
    await session.flush([assignment])
    session.add_all(
        RiderAssignmentItem(
            assignment_id=assignment.assignment_id,
            pickup_execution_id=pickup_id,
            assigned_at=assignment_time,
            released_at=None,
            release_reason_code=None,
        )
        for pickup_id in pickup_ids
    )
    pickup_result = await session.execute(
        update(PickupExecution)
        .where(
            PickupExecution.pickup_execution_id.in_(pickup_ids),
            PickupExecution.status == PICKUP_PENDING_ASSIGNMENT,
        )
        .values(status=PICKUP_ASSIGNED, updated_at=assignment_time)
    )
    if cast(CursorResult[Any], pickup_result).rowcount != len(pickup_ids):
        raise PickupGroupNotAssignableError("pickup state changed during assignment")

    rider_result = await session.execute(
        update(RiderAvailability)
        .where(
            RiderAvailability.rider_id == rider_id,
            RiderAvailability.availability_intent == INTENT_AVAILABLE,
            RiderAvailability.work_state == WORK_IDLE,
            RiderAvailability.version == availability.version,
        )
        .values(
            work_state=WORK_RESERVED,
            version=RiderAvailability.version + 1,
            updated_at=assignment_time,
        )
    )
    if cast(CursorResult[Any], rider_result).rowcount != 1:
        raise RiderNotEligibleError("rider availability changed during assignment")

    if offer is not None:
        offer.status = OFFER_ACCEPTED
        offer.responded_at = assignment_time
        offer.resolved_assignment_id = assignment.assignment_id
        await session.execute(
            update(AssignmentOffer)
            .where(
                AssignmentOffer.collection_group_id == collection_group_id,
                AssignmentOffer.offer_id != offer.offer_id,
                AssignmentOffer.status == OFFER_OPEN,
            )
            .values(
                status=OFFER_CLOSED_LOST,
                resolved_assignment_id=assignment.assignment_id,
            )
        )
    else:
        await session.execute(
            update(AssignmentOffer)
            .where(
                AssignmentOffer.collection_group_id == collection_group_id,
                AssignmentOffer.status == OFFER_OPEN,
            )
            .values(
                status=OFFER_CLOSED_LOST,
                resolved_assignment_id=assignment.assignment_id,
            )
        )

    await append_outbox_event(
        session,
        event_key=f"rider-assignment-created:{assignment.assignment_id}",
        aggregate_type="rider_assignment",
        aggregate_id=assignment.assignment_id,
        event_type=RIDER_ASSIGNMENT_CREATED,
        payload={
            "assignment_id": str(assignment.assignment_id),
            "collection_group_id": str(collection_group_id),
            "rider_id": str(rider_id),
        },
    )
    return assignment


async def _lock_group(session: AsyncSession, group_id: UUID) -> CollectionGroup:
    group = await session.scalar(
        select(CollectionGroup)
        .where(CollectionGroup.collection_group_id == group_id)
        .with_for_update()
    )
    if group is None:
        raise CollectionGroupNotFoundError("collection group not found")
    return group


async def _lock_assignment(session: AsyncSession, assignment_id: UUID) -> RiderAssignment:
    assignment = await session.scalar(
        select(RiderAssignment)
        .where(RiderAssignment.assignment_id == assignment_id)
        .with_for_update()
    )
    if assignment is None:
        raise AssignmentStateInconsistentError("offer resolving assignment is missing")
    return assignment


async def _require_initial_dispatch_population(
    session: AsyncSession, collection_group_id: UUID
) -> None:
    statuses = tuple(
        await session.scalars(
            select(PickupExecution.status).where(
                PickupExecution.collection_group_id == collection_group_id
            )
        )
    )
    if not statuses or any(status != PICKUP_PENDING_ASSIGNMENT for status in statuses):
        raise PickupGroupNotAssignableError(
            "new offers require an entirely PENDING_ASSIGNMENT group"
        )


async def _active_assignment(
    session: AsyncSession, group_id: UUID, *, lock: bool = False
) -> RiderAssignment | None:
    statement = select(RiderAssignment).where(
        RiderAssignment.collection_group_id == group_id,
        RiderAssignment.status == ASSIGNMENT_ACTIVE,
    )
    if lock:
        statement = statement.with_for_update()
    return cast(RiderAssignment | None, await session.scalar(statement))


async def _lock_rider(
    session: AsyncSession, rider_id: UUID
) -> tuple[RiderProfile, RiderAvailability]:
    profile = await session.scalar(
        select(RiderProfile).where(RiderProfile.rider_id == rider_id).with_for_update()
    )
    if profile is None:
        raise RiderNotFoundError("rider profile not found")
    availability = await session.scalar(
        select(RiderAvailability).where(RiderAvailability.rider_id == rider_id).with_for_update()
    )
    if availability is None:
        raise RiderNotFoundError("rider availability not found")
    return profile, availability


async def _lock_eligible_rider(
    session: AsyncSession, rider_id: UUID
) -> tuple[RiderProfile, RiderAvailability]:
    profile, availability = await _lock_rider(session, rider_id)
    _require_rider_eligible(profile, availability)
    await _require_fresh_rider_authorization(session, rider_id)
    return profile, availability


async def _require_fresh_rider_authorization(session: AsyncSession, rider_id: UUID) -> None:
    if not await lock_active_user_role(session, user_id=rider_id, role_code=ROLE_RIDER):
        raise RiderNotEligibleError("rider must be an ACTIVE user with an active RIDER role")


def _require_rider_eligible(profile: RiderProfile, availability: RiderAvailability) -> None:
    if (
        profile.status != RIDER_ACTIVE
        or availability.availability_intent != INTENT_AVAILABLE
        or availability.work_state != WORK_IDLE
    ):
        raise RiderNotEligibleError("rider must be ACTIVE, AVAILABLE, and IDLE")
