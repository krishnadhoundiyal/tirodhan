from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import (
    AssignmentOffer,
    RiderAssignment,
    RiderAssignmentItem,
    RiderAvailability,
    RiderProfile,
)
from tirodhan.modules.dispatch.service import (
    ASSIGNMENT_ACTIVE,
    INTENT_AVAILABLE,
    INTENT_OFFLINE,
    OFFER_ACCEPTED,
    OFFER_CLOSED_LOST,
    OFFER_OPEN,
    PICKUP_ASSIGNED,
    PICKUP_PENDING_ASSIGNMENT,
    RIDER_ACTIVE,
    RIDER_ASSIGNMENT_CREATED,
    RIDER_SUSPENDED,
    SOURCE_MANAGER_ASSIGNED,
    SOURCE_RIDER_OFFER_ACCEPTED,
    WORK_BUSY,
    WORK_IDLE,
    WORK_RESERVED,
    AssignmentOfferConflictError,
    AssignmentOfferExpiredError,
    AssignmentOfferNotFoundError,
    GroupAlreadyAssignedError,
    PickupGroupNotAssignableError,
    RiderAvailabilityVersionConflictError,
    RiderNotEligibleError,
    accept_assignment_offer,
    assign_group_manually,
    create_assignment_offer,
    set_rider_availability_intent,
)
from tirodhan.modules.identity.models import AppUser
from tirodhan.modules.planning.models import CollectionGroup, PickupExecution, PlanningBatch
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.serviceability.models import ServiceabilityContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@dataclass(frozen=True)
class DispatchFixture:
    group_ids: tuple[UUID, ...]
    pickup_ids: tuple[tuple[UUID, ...], ...]
    rider_ids: tuple[UUID, ...]
    manager_id: UUID


async def create_fixture(
    factory: async_sessionmaker[AsyncSession],
    *,
    group_count: int = 1,
    rider_count: int = 2,
    pickups_per_group: int = 2,
) -> DispatchFixture:
    now = utc_now().replace(microsecond=0)
    group_ids: list[UUID] = []
    pickup_sets: list[tuple[UUID, ...]] = []
    rider_ids: list[UUID] = []
    async with factory() as session, session.begin():
        customer = AppUser(status="ACTIVE")
        manager = AppUser(status="ACTIVE")
        session.add_all([customer, manager])
        await session.flush()
        batch = PlanningBatch(
            planning_batch_id=new_uuid7(),
            cell_id="dispatch-cell",
            slot_start=now + timedelta(hours=1),
            slot_end=now + timedelta(hours=2),
            status="COMPLETED",
            completion_mode="ALGORITHM_RESULT",
            max_attempts_snapshot=3,
            algorithm_version="bounded-greedy-diameter-v1",
            compaction_distance_m_snapshot=500,
            max_group_requests_snapshot=8,
            created_at=now,
            completed_at=now,
        )
        session.add(batch)
        await session.flush([batch])
        for _ in range(rider_count):
            user = AppUser(status="ACTIVE")
            session.add(user)
            await session.flush([user])
            profile = RiderProfile(
                rider_id=user.user_id,
                status=RIDER_ACTIVE,
                vehicle_type_code=None,
                capacity_class_code=None,
                created_at=now,
                updated_at=now,
            )
            session.add(profile)
            await session.flush([profile])
            session.add(
                RiderAvailability(
                    rider_id=user.user_id,
                    availability_intent=INTENT_AVAILABLE,
                    work_state=WORK_IDLE,
                    version=1,
                    updated_at=now,
                )
            )
            rider_ids.append(user.user_id)
        for group_index in range(group_count):
            group = CollectionGroup(
                collection_group_id=new_uuid7(),
                planning_batch_id=batch.planning_batch_id,
                planning_mode="COMPACTED" if pickups_per_group > 1 else "NORMAL_SINGLETON",
                created_at=now,
            )
            session.add(group)
            group_ids.append(group.collection_group_id)
            pickups: list[UUID] = []
            for pickup_index in range(pickups_per_group):
                context = ServiceabilityContext(
                    serviceability_context_id=new_uuid7(),
                    user_id=customer.user_id,
                    source_address_id=None,
                    source_address_version=None,
                    address_snapshot_encrypted=b"dispatch-test-envelope",
                    location=WKTElement("POINT(77.2090 28.6139)", srid=4326),
                    cell_id="dispatch-cell",
                    status="SERVICEABLE",
                    failure_code=None,
                    expires_at=now + timedelta(days=1),
                    created_at=now,
                    resolved_at=now,
                )
                session.add(context)
                await session.flush([context])
                request = CollectionRequest(
                    request_id=new_uuid7(),
                    client_request_id=new_uuid7(),
                    customer_id=customer.user_id,
                    serviceability_context_id=context.serviceability_context_id,
                    pickup_address_snapshot_encrypted=b"dispatch-test-envelope",
                    pickup_location=WKTElement("POINT(77.2090 28.6139)", srid=4326),
                    cell_id="dispatch-cell",
                    slot_start=batch.slot_start,
                    slot_end=batch.slot_end,
                    quoted_amount_minor=1000 + group_index + pickup_index,
                    currency="INR",
                    status="PLANNED",
                    payment_expires_at=now + timedelta(minutes=30),
                    planning_batch_id=batch.planning_batch_id,
                    created_at=now,
                    accepted_at=now,
                    cancelled_at=None,
                    completed_at=None,
                    expired_at=None,
                )
                session.add(request)
                await session.flush([request])
                pickup = PickupExecution(
                    pickup_execution_id=new_uuid7(),
                    request_id=request.request_id,
                    collection_group_id=group.collection_group_id,
                    status=PICKUP_PENDING_ASSIGNMENT,
                    created_at=now,
                    collected_at=None,
                    completed_at=None,
                    updated_at=now,
                )
                session.add(pickup)
                pickups.append(pickup.pickup_execution_id)
            pickup_sets.append(tuple(pickups))
    return DispatchFixture(tuple(group_ids), tuple(pickup_sets), tuple(rider_ids), manager.user_id)


async def create_offer(
    factory: async_sessionmaker[AsyncSession],
    fixture: DispatchFixture,
    *,
    group_index: int = 0,
    rider_index: int = 0,
    offer_round: int = 1,
    now: datetime | None = None,
) -> AssignmentOffer:
    offered_at = now or utc_now()
    return await create_assignment_offer(
        factory,
        collection_group_id=fixture.group_ids[group_index],
        rider_id=fixture.rider_ids[rider_index],
        offer_round=offer_round,
        expires_at=offered_at + timedelta(minutes=5),
        now=offered_at,
    )


async def test_availability_intent_uses_version_without_changing_work_state(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory)
    rider_id = fixture.rider_ids[0]
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(RiderAvailability)
            .where(RiderAvailability.rider_id == rider_id)
            .values(work_state=WORK_BUSY)
        )

    offline = await set_rider_availability_intent(
        database_session_factory,
        rider_id=rider_id,
        intent=INTENT_OFFLINE,
        expected_version=1,
    )
    assert (offline.availability_intent, offline.work_state, offline.version) == (
        INTENT_OFFLINE,
        WORK_BUSY,
        2,
    )
    with pytest.raises(RiderAvailabilityVersionConflictError):
        await set_rider_availability_intent(
            database_session_factory,
            rider_id=rider_id,
            intent=INTENT_AVAILABLE,
            expected_version=1,
        )
    available = await set_rider_availability_intent(
        database_session_factory,
        rider_id=rider_id,
        intent=INTENT_AVAILABLE,
        expected_version=2,
    )
    assert (available.availability_intent, available.work_state, available.version) == (
        INTENT_AVAILABLE,
        WORK_BUSY,
        3,
    )


async def test_offer_creation_replay_and_conflicting_expiry(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory)
    now = utc_now().replace(microsecond=0)
    expiry = now + timedelta(minutes=5)
    first = await create_assignment_offer(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        offer_round=1,
        expires_at=expiry,
        now=now,
    )
    replay = await create_assignment_offer(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        offer_round=1,
        expires_at=expiry,
        now=now + timedelta(hours=1),
    )
    assert replay.offer_id == first.offer_id
    with pytest.raises(AssignmentOfferConflictError):
        await create_assignment_offer(
            database_session_factory,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[0],
            offer_round=1,
            expires_at=expiry + timedelta(seconds=1),
            now=now,
        )
    async with database_session_factory() as session:
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert availability is not None
        assert availability.work_state == WORK_IDLE
        assert availability.version == 1


@pytest.mark.parametrize(
    ("profile_status", "intent", "work_state"),
    [
        (RIDER_SUSPENDED, INTENT_AVAILABLE, WORK_IDLE),
        (RIDER_ACTIVE, INTENT_OFFLINE, WORK_IDLE),
        (RIDER_ACTIVE, INTENT_AVAILABLE, WORK_RESERVED),
        (RIDER_ACTIVE, INTENT_AVAILABLE, WORK_BUSY),
    ],
)
async def test_offer_creation_rejects_ineligible_rider(
    database_session_factory: async_sessionmaker[AsyncSession],
    profile_status: str,
    intent: str,
    work_state: str,
) -> None:
    fixture = await create_fixture(database_session_factory)
    async with database_session_factory() as session, session.begin():
        profile = await session.get(RiderProfile, fixture.rider_ids[0])
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert profile is not None and availability is not None
        profile.status = profile_status
        availability.availability_intent = intent
        availability.work_state = work_state
    with pytest.raises(RiderNotEligibleError):
        await create_offer(database_session_factory, fixture)


async def test_offer_creation_rejects_bad_round_expiry_and_assigned_group(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory)
    now = utc_now()
    with pytest.raises(ValueError):
        await create_assignment_offer(
            database_session_factory,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[0],
            offer_round=0,
            expires_at=now + timedelta(minutes=1),
            now=now,
        )
    with pytest.raises(ValueError):
        await create_assignment_offer(
            database_session_factory,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[0],
            offer_round=1,
            expires_at=now,
            now=now,
        )
    await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    with pytest.raises(GroupAlreadyAssignedError):
        await create_assignment_offer(
            database_session_factory,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[1],
            offer_round=1,
            expires_at=now + timedelta(minutes=5),
            now=now,
        )


async def test_offer_acceptance_assigns_full_group_atomically(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory)
    winner = await create_offer(database_session_factory, fixture, rider_index=0)
    sibling = await create_offer(database_session_factory, fixture, rider_index=1)

    assignment = await accept_assignment_offer(
        database_session_factory, offer_id=winner.offer_id, rider_id=fixture.rider_ids[0]
    )
    async with database_session_factory() as session:
        items = list(
            await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == assignment.assignment_id
                )
            )
        )
        pickups = list(
            await session.scalars(
                select(PickupExecution).where(
                    PickupExecution.collection_group_id == fixture.group_ids[0]
                )
            )
        )
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        persisted_winner = await session.get(AssignmentOffer, winner.offer_id)
        persisted_sibling = await session.get(AssignmentOffer, sibling.offer_id)
        events = list(
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == RIDER_ASSIGNMENT_CREATED)
            )
        )
    assert assignment.status == ASSIGNMENT_ACTIVE
    assert assignment.source == SOURCE_RIDER_OFFER_ACCEPTED
    assert assignment.assigned_by_user_id is None
    assert {item.pickup_execution_id for item in items} == set(fixture.pickup_ids[0])
    assert all(item.released_at is None for item in items)
    assert all(pickup.status == PICKUP_ASSIGNED for pickup in pickups)
    assert availability is not None
    assert (availability.work_state, availability.version) == (WORK_RESERVED, 2)
    assert persisted_winner is not None and persisted_winner.status == OFFER_ACCEPTED
    assert persisted_winner.responded_at is not None
    assert persisted_sibling is not None and persisted_sibling.status == OFFER_CLOSED_LOST
    assert persisted_sibling.responded_at is None
    assert len(events) == 1
    assert events[0].payload == {
        "assignment_id": str(assignment.assignment_id),
        "collection_group_id": str(fixture.group_ids[0]),
        "rider_id": str(fixture.rider_ids[0]),
    }


async def test_accepted_offer_replay_has_no_duplicate_effects(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory)
    offer = await create_offer(database_session_factory, fixture)
    first = await accept_assignment_offer(
        database_session_factory, offer_id=offer.offer_id, rider_id=fixture.rider_ids[0]
    )
    replay = await accept_assignment_offer(
        database_session_factory, offer_id=offer.offer_id, rider_id=fixture.rider_ids[0]
    )
    assert replay.assignment_id == first.assignment_id
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1
        assert await session.scalar(select(func.count()).select_from(RiderAssignmentItem)) == 2
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.outbox_event_id)).where(
                    OutboxEvent.event_type == RIDER_ASSIGNMENT_CREATED
                )
            )
            == 1
        )
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert availability is not None and availability.version == 2


async def test_manual_assignment_replays_same_rider_and_conflicts_other(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory)
    offer = await create_offer(database_session_factory, fixture, rider_index=1)
    first = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    replay = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    assert replay.assignment_id == first.assignment_id
    assert first.source == SOURCE_MANAGER_ASSIGNED
    assert first.assigned_by_user_id == fixture.manager_id
    with pytest.raises(GroupAlreadyAssignedError):
        await assign_group_manually(
            database_session_factory,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
        )
    async with database_session_factory() as session:
        persisted_offer = await session.get(AssignmentOffer, offer.offer_id)
        assert persisted_offer is not None and persisted_offer.status == OFFER_CLOSED_LOST


async def test_acceptance_rejects_wrong_missing_expired_and_closed_offer(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory)
    now = utc_now().replace(microsecond=0)
    offer = await create_offer(database_session_factory, fixture, now=now)
    with pytest.raises(AssignmentOfferConflictError):
        await accept_assignment_offer(
            database_session_factory, offer_id=offer.offer_id, rider_id=fixture.rider_ids[1]
        )
    with pytest.raises(AssignmentOfferNotFoundError):
        await accept_assignment_offer(
            database_session_factory, offer_id=new_uuid7(), rider_id=fixture.rider_ids[0]
        )
    with pytest.raises(AssignmentOfferExpiredError):
        await accept_assignment_offer(
            database_session_factory,
            offer_id=offer.offer_id,
            rider_id=fixture.rider_ids[0],
            now=offer.expires_at,
        )
    async with database_session_factory() as session, session.begin():
        persisted = await session.get(AssignmentOffer, offer.offer_id)
        assert persisted is not None
        persisted.status = OFFER_CLOSED_LOST
    with pytest.raises(GroupAlreadyAssignedError):
        await accept_assignment_offer(
            database_session_factory, offer_id=offer.offer_id, rider_id=fixture.rider_ids[0]
        )


@pytest.mark.parametrize(
    ("profile_status", "intent", "work_state"),
    [
        (RIDER_SUSPENDED, INTENT_AVAILABLE, WORK_IDLE),
        (RIDER_ACTIVE, INTENT_OFFLINE, WORK_IDLE),
        (RIDER_ACTIVE, INTENT_AVAILABLE, WORK_RESERVED),
        (RIDER_ACTIVE, INTENT_AVAILABLE, WORK_BUSY),
    ],
)
async def test_acceptance_rechecks_rider_eligibility(
    database_session_factory: async_sessionmaker[AsyncSession],
    profile_status: str,
    intent: str,
    work_state: str,
) -> None:
    fixture = await create_fixture(database_session_factory)
    offer = await create_offer(database_session_factory, fixture)
    async with database_session_factory() as session, session.begin():
        profile = await session.get(RiderProfile, fixture.rider_ids[0])
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert profile is not None and availability is not None
        profile.status = profile_status
        availability.availability_intent = intent
        availability.work_state = work_state
    with pytest.raises(RiderNotEligibleError):
        await accept_assignment_offer(
            database_session_factory, offer_id=offer.offer_id, rider_id=fixture.rider_ids[0]
        )


async def test_assignment_rejects_empty_or_nonpending_group(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    empty = await create_fixture(database_session_factory, pickups_per_group=0)
    with pytest.raises(PickupGroupNotAssignableError):
        await assign_group_manually(
            database_session_factory,
            collection_group_id=empty.group_ids[0],
            rider_id=empty.rider_ids[0],
            manager_user_id=empty.manager_id,
        )


async def test_nonpending_pickup_and_existing_ownership_are_rejected(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory, group_count=2, rider_count=2)
    async with database_session_factory() as session, session.begin():
        pickup = await session.get(PickupExecution, fixture.pickup_ids[0][0])
        assert pickup is not None
        pickup.status = PICKUP_ASSIGNED
    with pytest.raises(PickupGroupNotAssignableError):
        await assign_group_manually(
            database_session_factory,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[0],
            manager_user_id=fixture.manager_id,
        )
    async with database_session_factory() as session, session.begin():
        pickup = await session.get(PickupExecution, fixture.pickup_ids[0][0])
        assert pickup is not None
        pickup.status = PICKUP_PENDING_ASSIGNMENT

    other = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[1],
        rider_id=fixture.rider_ids[1],
        manager_user_id=fixture.manager_id,
    )
    async with database_session_factory() as session, session.begin():
        session.add(
            RiderAssignmentItem(
                assignment_id=other.assignment_id,
                pickup_execution_id=fixture.pickup_ids[0][1],
                assigned_at=utc_now(),
                released_at=None,
                release_reason_code=None,
            )
        )
    with pytest.raises(PickupGroupNotAssignableError):
        await assign_group_manually(
            database_session_factory,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[0],
            manager_user_id=fixture.manager_id,
        )


async def test_two_riders_concurrently_accept_same_group(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory)
    offers = [
        await create_offer(database_session_factory, fixture, rider_index=index)
        for index in range(2)
    ]
    results = await asyncio.gather(
        *(
            accept_assignment_offer(
                database_session_factory,
                offer_id=offers[index].offer_id,
                rider_id=fixture.rider_ids[index],
            )
            for index in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RiderAssignment) for result in results) == 1
    assert sum(isinstance(result, GroupAlreadyAssignedError) for result in results) == 1
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1
        states = list(await session.scalars(select(RiderAvailability.work_state)))
        assert states.count(WORK_RESERVED) == 1
        statuses = list(await session.scalars(select(AssignmentOffer.status)))
        assert sorted(statuses) == sorted([OFFER_ACCEPTED, OFFER_CLOSED_LOST])


async def test_one_rider_concurrently_competes_for_two_groups(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory, group_count=2, rider_count=1)
    results = await asyncio.gather(
        *(
            assign_group_manually(
                database_session_factory,
                collection_group_id=group_id,
                rider_id=fixture.rider_ids[0],
                manager_user_id=fixture.manager_id,
            )
            for group_id in fixture.group_ids
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RiderAssignment) for result in results) == 1
    assert sum(isinstance(result, RiderNotEligibleError) for result in results) == 1


async def test_manager_and_offer_acceptance_race(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory)
    offer = await create_offer(database_session_factory, fixture, rider_index=0)
    results = await asyncio.gather(
        accept_assignment_offer(
            database_session_factory, offer_id=offer.offer_id, rider_id=fixture.rider_ids[0]
        ),
        assign_group_manually(
            database_session_factory,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RiderAssignment) for result in results) == 1
    assert sum(isinstance(result, GroupAlreadyAssignedError) for result in results) == 1


async def test_partial_unique_indexes_are_physical_backstops(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory, group_count=2)
    first = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    now = utc_now()
    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            session.add(
                RiderAssignment(
                    assignment_id=new_uuid7(),
                    collection_group_id=fixture.group_ids[0],
                    rider_id=fixture.rider_ids[1],
                    source=SOURCE_MANAGER_ASSIGNED,
                    status=ASSIGNMENT_ACTIVE,
                    assigned_by_user_id=fixture.manager_id,
                    supersedes_assignment_id=None,
                    created_at=now,
                    assigned_at=now,
                    started_at=None,
                    completed_at=None,
                )
            )
            await session.flush()
    second = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[1],
        rider_id=fixture.rider_ids[1],
        manager_user_id=fixture.manager_id,
    )
    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            session.add(
                RiderAssignmentItem(
                    assignment_id=second.assignment_id,
                    pickup_execution_id=fixture.pickup_ids[0][0],
                    assigned_at=now,
                    released_at=None,
                    release_reason_code=None,
                )
            )
            await session.flush()
    assert first.assignment_id != second.assignment_id


async def test_late_failure_rolls_back_every_assignment_effect(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await create_fixture(database_session_factory)
    winner = await create_offer(database_session_factory, fixture, rider_index=0)
    sibling = await create_offer(database_session_factory, fixture, rider_index=1)

    async def fail_outbox(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr("tirodhan.modules.dispatch.service.append_outbox_event", fail_outbox)
    with pytest.raises(RuntimeError, match="injected outbox failure"):
        await accept_assignment_offer(
            database_session_factory, offer_id=winner.offer_id, rider_id=fixture.rider_ids[0]
        )

    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 0
        assert await session.scalar(select(func.count()).select_from(RiderAssignmentItem)) == 0
        pickups = list(await session.scalars(select(PickupExecution.status)))
        assert set(pickups) == {PICKUP_PENDING_ASSIGNMENT}
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert availability is not None
        assert (availability.work_state, availability.version) == (WORK_IDLE, 1)
        offers = {
            offer.offer_id: offer
            for offer in await session.scalars(
                select(AssignmentOffer).where(
                    AssignmentOffer.offer_id.in_([winner.offer_id, sibling.offer_id])
                )
            )
        }
        assert offers[winner.offer_id].status == OFFER_OPEN
        assert offers[sibling.offer_id].status == OFFER_OPEN
        assert (
            await session.scalar(
                select(func.count(OutboxEvent.outbox_event_id)).where(
                    OutboxEvent.event_type == RIDER_ASSIGNMENT_CREATED
                )
            )
            == 0
        )


async def test_outbox_payload_contains_only_control_identifiers(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(database_session_factory)
    await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    async with database_session_factory() as session:
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == RIDER_ASSIGNMENT_CREATED)
        )
    assert event is not None
    assert set(event.payload) == {"assignment_id", "collection_group_id", "rider_id"}
    serialized = str(event.payload).lower()
    assert all(
        prohibited not in serialized
        for prohibited in ("address", "coordinate", "phone", "customer", "payment", "location")
    )
