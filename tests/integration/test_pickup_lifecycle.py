from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_rider_dispatch import DispatchFixture, create_fixture

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import (
    RiderAssignment,
    RiderAssignmentItem,
    RiderAvailability,
    RiderProfile,
)
from tirodhan.modules.dispatch.service import (
    INTENT_AVAILABLE,
    INTENT_OFFLINE,
    RIDER_SUSPENDED,
    WORK_BUSY,
    WORK_IDLE,
    WORK_RESERVED,
    assign_group_manually,
    set_rider_availability_intent,
)
from tirodhan.modules.pickups.models import PickupAttempt
from tirodhan.modules.pickups.service import (
    ASSIGNMENT_ACTIVE,
    ASSIGNMENT_COMPLETED,
    ATTEMPT_COLLECTED,
    ATTEMPT_NOT_COLLECTED,
    PICKUP_ASSIGNED,
    PICKUP_COLLECTED,
    AssignmentNotFoundError,
    AssignmentNotStartableError,
    AssignmentStateInconsistentError,
    AssignmentWrongRiderError,
    PickupAttemptReplayConflictError,
    PickupExecutionNotFoundError,
    PickupNotAttemptableError,
    PickupOwnershipMismatchError,
    RiderWorkStateInvalidError,
    record_pickup_attempt,
    start_assignment,
)
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.reliability.models import OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def assigned_fixture(
    factory: async_sessionmaker[AsyncSession],
    *,
    pickup_count: int = 2,
) -> tuple[DispatchFixture, RiderAssignment]:
    fixture = await create_fixture(
        factory,
        group_count=1,
        rider_count=2,
        pickups_per_group=pickup_count,
    )
    assignment = await assign_group_manually(
        factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    return fixture, assignment


async def started_fixture(
    factory: async_sessionmaker[AsyncSession],
    *,
    pickup_count: int = 2,
) -> tuple[DispatchFixture, RiderAssignment]:
    fixture, assignment = await assigned_fixture(factory, pickup_count=pickup_count)
    started = await start_assignment(
        factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    return fixture, started


async def test_start_assignment_transitions_reserved_rider_once(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await assigned_fixture(database_session_factory)
    start_time = utc_now().replace(microsecond=0)

    started = await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
        now=start_time,
    )
    replay = await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
        now=start_time + timedelta(minutes=1),
    )

    async with database_session_factory() as session:
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
    assert started.started_at == start_time
    assert replay.started_at == start_time
    assert availability is not None
    assert (
        availability.availability_intent,
        availability.work_state,
        availability.version,
    ) == (INTENT_AVAILABLE, WORK_BUSY, 3)


async def test_offline_reserved_rider_can_start_existing_work(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await assigned_fixture(database_session_factory)
    await set_rider_availability_intent(
        database_session_factory,
        rider_id=fixture.rider_ids[0],
        intent=INTENT_OFFLINE,
        expected_version=2,
    )

    await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    async with database_session_factory() as session:
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
    assert availability is not None
    assert (availability.availability_intent, availability.work_state, availability.version) == (
        INTENT_OFFLINE,
        WORK_BUSY,
        4,
    )


async def test_suspended_rider_cannot_start_unstarted_assignment(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await assigned_fixture(database_session_factory)
    async with database_session_factory() as session, session.begin():
        profile = await session.get(RiderProfile, fixture.rider_ids[0])
        assert profile is not None
        profile.status = RIDER_SUSPENDED
    with pytest.raises(AssignmentNotStartableError):
        await start_assignment(
            database_session_factory,
            assignment_id=assignment.assignment_id,
            rider_id=fixture.rider_ids[0],
        )


async def test_start_rejects_missing_wrong_rider_and_inconsistent_replay(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await assigned_fixture(database_session_factory)
    with pytest.raises(AssignmentNotFoundError):
        await start_assignment(
            database_session_factory,
            assignment_id=new_uuid7(),
            rider_id=fixture.rider_ids[0],
        )
    with pytest.raises(AssignmentWrongRiderError):
        await start_assignment(
            database_session_factory,
            assignment_id=assignment.assignment_id,
            rider_id=fixture.rider_ids[1],
        )
    await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    async with database_session_factory() as session, session.begin():
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert availability is not None
        availability.work_state = WORK_RESERVED
    with pytest.raises(AssignmentStateInconsistentError):
        await start_assignment(
            database_session_factory,
            assignment_id=assignment.assignment_id,
            rider_id=fixture.rider_ids[0],
        )


async def test_failed_then_successful_attempts_are_sequenced_and_replayable(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await started_fixture(database_session_factory, pickup_count=1)
    pickup_id = fixture.pickup_ids[0][0]
    failed_client_id = new_uuid7()
    collected_client_id = new_uuid7()

    first = await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=failed_client_id,
        outcome=ATTEMPT_NOT_COLLECTED,
    )
    async with database_session_factory() as session:
        pickup_after_failure = await session.get(PickupExecution, pickup_id)
        assignment_after_failure = await session.get(RiderAssignment, assignment.assignment_id)
        availability_after_failure = await session.get(RiderAvailability, fixture.rider_ids[0])
    assert pickup_after_failure is not None
    assert (pickup_after_failure.status, pickup_after_failure.collected_at) == (
        PICKUP_ASSIGNED,
        None,
    )
    assert assignment_after_failure is not None
    assert (assignment_after_failure.status, assignment_after_failure.completed_at) == (
        ASSIGNMENT_ACTIVE,
        None,
    )
    assert availability_after_failure is not None
    assert availability_after_failure.work_state == WORK_BUSY
    second = await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_NOT_COLLECTED,
    )
    collected_at = utc_now().replace(microsecond=0)
    success = await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=collected_client_id,
        outcome=ATTEMPT_COLLECTED,
        now=collected_at,
    )
    failed_replay = await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=failed_client_id,
        outcome=ATTEMPT_NOT_COLLECTED,
    )
    success_replay = await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=collected_client_id,
        outcome=ATTEMPT_COLLECTED,
        now=collected_at + timedelta(minutes=1),
    )

    assert (first.attempt_number, second.attempt_number, success.attempt_number) == (1, 2, 3)
    assert failed_replay.pickup_attempt_id == first.pickup_attempt_id
    assert success_replay.pickup_attempt_id == success.pickup_attempt_id
    async with database_session_factory() as session:
        pickup = await session.get(PickupExecution, pickup_id)
        persisted_assignment = await session.get(RiderAssignment, assignment.assignment_id)
        request = await session.get(CollectionRequest, pickup.request_id if pickup else None)
        attempts = list(
            await session.scalars(
                select(PickupAttempt)
                .where(PickupAttempt.pickup_execution_id == pickup_id)
                .order_by(PickupAttempt.attempt_number)
            )
        )
    assert len(attempts) == 3
    assert all(
        attempt.rider_assignment_id == assignment.assignment_id
        and attempt.attempted_at == attempt.created_at
        for attempt in attempts
    )
    assert [attempt.outcome for attempt in attempts] == [
        ATTEMPT_NOT_COLLECTED,
        ATTEMPT_NOT_COLLECTED,
        ATTEMPT_COLLECTED,
    ]
    assert pickup is not None
    assert (pickup.status, pickup.collected_at, pickup.completed_at) == (
        PICKUP_COLLECTED,
        collected_at,
        None,
    )
    assert persisted_assignment is not None
    assert persisted_assignment.status == ASSIGNMENT_COMPLETED
    assert request is not None and request.status == "PLANNED"


async def test_attempt_replay_conflict_and_wrong_rider_are_rejected(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await started_fixture(database_session_factory)
    pickup_id = fixture.pickup_ids[0][0]
    client_id = new_uuid7()
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=client_id,
        outcome=ATTEMPT_NOT_COLLECTED,
    )
    with pytest.raises(PickupAttemptReplayConflictError):
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_attempt_id=client_id,
            outcome=ATTEMPT_COLLECTED,
        )
    with pytest.raises(AssignmentWrongRiderError):
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[1],
            client_attempt_id=client_id,
            outcome=ATTEMPT_NOT_COLLECTED,
        )


async def test_fresh_attempt_requires_started_busy_current_ownership(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await assigned_fixture(database_session_factory)
    pickup_id = fixture.pickup_ids[0][0]
    with pytest.raises(AssignmentNotStartableError):
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_NOT_COLLECTED,
        )
    await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    async with database_session_factory() as session, session.begin():
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert availability is not None
        availability.work_state = WORK_IDLE
    with pytest.raises(RiderWorkStateInvalidError):
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_NOT_COLLECTED,
        )

    async with database_session_factory() as session, session.begin():
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        ownership = await session.get(RiderAssignmentItem, (assignment.assignment_id, pickup_id))
        assert availability is not None and ownership is not None
        availability.work_state = WORK_BUSY
        ownership.released_at = utc_now()
        ownership.release_reason_code = "TEST_RELEASE"
    with pytest.raises(PickupOwnershipMismatchError):
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_NOT_COLLECTED,
        )
    with pytest.raises(PickupExecutionNotFoundError):
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=new_uuid7(),
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_NOT_COLLECTED,
        )


async def test_fresh_attempt_after_collected_pickup_is_rejected(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await started_fixture(database_session_factory, pickup_count=2)
    pickup_id = fixture.pickup_ids[0][0]
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_COLLECTED,
    )
    with pytest.raises(PickupNotAttemptableError):
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_NOT_COLLECTED,
        )


async def test_suspended_after_start_does_not_block_pickup_attempt(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await started_fixture(database_session_factory)
    async with database_session_factory() as session, session.begin():
        profile = await session.get(RiderProfile, fixture.rider_ids[0])
        assert profile is not None
        profile.status = RIDER_SUSPENDED
    attempt = await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=fixture.pickup_ids[0][0],
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_NOT_COLLECTED,
    )
    assert attempt.attempt_number == 1


async def test_multi_pickup_completion_preserves_intent_items_and_outbox(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await assigned_fixture(database_session_factory, pickup_count=2)
    await set_rider_availability_intent(
        database_session_factory,
        rider_id=fixture.rider_ids[0],
        intent=INTENT_OFFLINE,
        expected_version=2,
    )
    await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    first_time = utc_now().replace(microsecond=0)
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=fixture.pickup_ids[0][0],
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_COLLECTED,
        now=first_time,
    )
    async with database_session_factory() as session:
        intermediate = await session.get(RiderAssignment, assignment.assignment_id)
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
    assert intermediate is not None
    assert (intermediate.status, intermediate.completed_at) == (ASSIGNMENT_ACTIVE, None)
    assert availability is not None
    assert (availability.availability_intent, availability.work_state, availability.version) == (
        INTENT_OFFLINE,
        WORK_BUSY,
        4,
    )

    completion_time = first_time + timedelta(minutes=1)
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=fixture.pickup_ids[0][1],
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_COLLECTED,
        now=completion_time,
    )
    replayed = await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    async with database_session_factory() as session:
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        items = list(
            await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == assignment.assignment_id
                )
            )
        )
        outbox_types = list(await session.scalars(select(OutboxEvent.event_type)))
    assert replayed.status == ASSIGNMENT_COMPLETED
    assert replayed.completed_at == completion_time
    assert availability is not None
    assert (availability.availability_intent, availability.work_state, availability.version) == (
        INTENT_OFFLINE,
        WORK_IDLE,
        5,
    )
    assert all(item.released_at is None and item.release_reason_code is None for item in items)
    assert outbox_types == ["RiderAssignmentCreated"]


async def test_concurrent_duplicate_attempt_converges_to_one_row(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await started_fixture(database_session_factory)
    client_id = new_uuid7()
    results = await asyncio.gather(
        *(
            record_pickup_attempt(
                database_session_factory,
                pickup_execution_id=fixture.pickup_ids[0][0],
                rider_id=fixture.rider_ids[0],
                client_attempt_id=client_id,
                outcome=ATTEMPT_NOT_COLLECTED,
            )
            for _ in range(2)
        )
    )
    assert results[0].pickup_attempt_id == results[1].pickup_attempt_id
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PickupAttempt)) == 1


async def test_concurrent_distinct_attempts_receive_sequential_numbers(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await started_fixture(database_session_factory)
    results = await asyncio.gather(
        *(
            record_pickup_attempt(
                database_session_factory,
                pickup_execution_id=fixture.pickup_ids[0][0],
                rider_id=fixture.rider_ids[0],
                client_attempt_id=new_uuid7(),
                outcome=ATTEMPT_NOT_COLLECTED,
            )
            for _ in range(2)
        )
    )
    assert {result.attempt_number for result in results} == {1, 2}


async def test_concurrent_final_pickups_complete_assignment_once(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await started_fixture(database_session_factory, pickup_count=2)
    await asyncio.gather(
        *(
            record_pickup_attempt(
                database_session_factory,
                pickup_execution_id=pickup_id,
                rider_id=fixture.rider_ids[0],
                client_attempt_id=new_uuid7(),
                outcome=ATTEMPT_COLLECTED,
            )
            for pickup_id in fixture.pickup_ids[0]
        )
    )
    async with database_session_factory() as session:
        persisted_assignment = await session.get(RiderAssignment, assignment.assignment_id)
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        pickup_states = list(
            await session.scalars(
                select(PickupExecution.status).where(
                    PickupExecution.pickup_execution_id.in_(fixture.pickup_ids[0])
                )
            )
        )
        assert await session.scalar(select(func.count()).select_from(PickupAttempt)) == 2
    assert persisted_assignment is not None
    assert persisted_assignment.status == ASSIGNMENT_COMPLETED
    assert persisted_assignment.completed_at is not None
    assert availability is not None
    assert (availability.work_state, availability.version) == (WORK_IDLE, 4)
    assert pickup_states == [PICKUP_COLLECTED, PICKUP_COLLECTED]


async def test_late_failure_rolls_back_final_collection(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, assignment = await started_fixture(database_session_factory, pickup_count=1)
    pickup_id = fixture.pickup_ids[0][0]
    from tirodhan.modules.pickups import service as pickup_service

    original = pickup_service._complete_assignment_if_finished

    async def complete_then_fail(*args: object, **kwargs: object) -> bool:
        await original(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("injected late pickup failure")

    monkeypatch.setattr(pickup_service, "_complete_assignment_if_finished", complete_then_fail)
    with pytest.raises(RuntimeError, match="injected late pickup failure"):
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_COLLECTED,
        )

    async with database_session_factory() as session:
        pickup = await session.get(PickupExecution, pickup_id)
        persisted_assignment = await session.get(RiderAssignment, assignment.assignment_id)
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert await session.scalar(select(func.count()).select_from(PickupAttempt)) == 0
    assert pickup is not None
    assert (pickup.status, pickup.collected_at) == (PICKUP_ASSIGNED, None)
    assert persisted_assignment is not None
    assert (persisted_assignment.status, persisted_assignment.completed_at) == (
        ASSIGNMENT_ACTIVE,
        None,
    )
    assert availability is not None
    assert (availability.work_state, availability.version) == (WORK_BUSY, 3)
