from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_rider_dispatch import DispatchFixture, create_fixture

from tirodhan.db.values import new_uuid7, utc_now
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
    SOURCE_MANAGER_ASSIGNED,
    WORK_BUSY,
    WORK_IDLE,
    WORK_RESERVED,
    RiderNotEligibleError,
    assign_group_manually,
    set_rider_availability_intent,
)
from tirodhan.modules.operations import service as operations_service
from tirodhan.modules.operations.service import (
    ASSIGNMENT_ACTIVE,
    ASSIGNMENT_COMPLETED,
    ASSIGNMENT_SUPERSEDED,
    INCIDENT_OPEN,
    INCIDENT_RESOLVED,
    RELEASE_REASSIGNED,
    RESOLUTION_REASSIGNED,
    PickupIncidentConflictError,
    PickupIncidentStateError,
    ReassignmentConflictError,
    ReassignmentRiderError,
    ReassignmentStateError,
    open_pickup_incident,
    reassign_outstanding_work,
)
from tirodhan.modules.pickups.models import PickupAttempt, PickupIncident
from tirodhan.modules.pickups.service import (
    ATTEMPT_COLLECTED,
    ATTEMPT_NOT_COLLECTED,
    PICKUP_ASSIGNED,
    PICKUP_COLLECTED,
    AssignmentNotStartableError,
    record_pickup_attempt,
    start_assignment,
)
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.reliability.models import IdempotencyRecord, OutboxEvent
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def assigned_fixture(
    factory: async_sessionmaker[AsyncSession],
    *,
    pickup_count: int = 2,
    rider_count: int = 3,
    started: bool = False,
) -> tuple[DispatchFixture, RiderAssignment]:
    fixture = await create_fixture(
        factory,
        group_count=1,
        rider_count=rider_count,
        pickups_per_group=pickup_count,
    )
    assignment = await assign_group_manually(
        factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    if started:
        assignment = await start_assignment(
            factory,
            assignment_id=assignment.assignment_id,
            rider_id=fixture.rider_ids[0],
        )
    return fixture, assignment


async def reassign(
    factory: async_sessionmaker[AsyncSession],
    fixture: DispatchFixture,
    predecessor: RiderAssignment,
    *,
    replacement_index: int = 1,
    command_id: UUID | None = None,
    incident_id: UUID | None = None,
) -> RiderAssignment:
    return await reassign_outstanding_work(
        factory,
        predecessor_assignment_id=predecessor.assignment_id,
        replacement_rider_id=fixture.rider_ids[replacement_index],
        manager_user_id=fixture.manager_id,
        client_reassignment_id=command_id or new_uuid7(),
        incident_id=incident_id,
        idempotency_expires_at=utc_now() + timedelta(days=1),
    )


async def test_incident_creation_replay_conflicts_and_preserves_execution_state(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await assigned_fixture(database_session_factory, started=True)
    pickup_id, other_pickup_id = fixture.pickup_ids[0]
    client_id = new_uuid7()
    await set_rider_availability_intent(
        database_session_factory,
        rider_id=fixture.rider_ids[0],
        intent=INTENT_OFFLINE,
        expected_version=3,
    )
    incident = await open_pickup_incident(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_incident_id=client_id,
        reason_code="CUSTOMER_UNAVAILABLE",
    )
    replay = await open_pickup_incident(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_incident_id=client_id,
        reason_code="CUSTOMER_UNAVAILABLE",
    )
    with pytest.raises(PickupIncidentConflictError):
        await open_pickup_incident(
            database_session_factory,
            pickup_execution_id=other_pickup_id,
            rider_id=fixture.rider_ids[0],
            client_incident_id=client_id,
            reason_code="CUSTOMER_UNAVAILABLE",
        )
    with pytest.raises(PickupIncidentConflictError):
        await open_pickup_incident(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_incident_id=client_id,
            reason_code="OTHER",
        )
    with pytest.raises(PickupIncidentConflictError):
        await open_pickup_incident(
            database_session_factory,
            pickup_execution_id=other_pickup_id,
            rider_id=fixture.rider_ids[1],
            client_incident_id=new_uuid7(),
            reason_code="OTHER",
        )
    second = await open_pickup_incident(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_incident_id=new_uuid7(),
        reason_code="OTHER",
    )

    async with database_session_factory() as session:
        pickup_states = list(
            await session.scalars(
                select(PickupExecution.status).where(
                    PickupExecution.pickup_execution_id.in_(fixture.pickup_ids[0])
                )
            )
        )
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        count = await session.scalar(select(func.count()).select_from(PickupIncident))
    assert replay.incident_id == incident.incident_id
    assert incident.rider_assignment_id == assignment.assignment_id
    assert incident.status == INCIDENT_OPEN
    assert second.incident_id != incident.incident_id
    assert count == 2
    assert pickup_states == [PICKUP_ASSIGNED, PICKUP_ASSIGNED]
    assert availability is not None
    assert (availability.availability_intent, availability.work_state, availability.version) == (
        INTENT_OFFLINE,
        WORK_BUSY,
        4,
    )


async def test_concurrent_exact_incident_command_converges(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assigned_fixture(
        database_session_factory, pickup_count=1, started=True
    )
    client_id = new_uuid7()
    incidents = await asyncio.gather(
        *(
            open_pickup_incident(
                database_session_factory,
                pickup_execution_id=fixture.pickup_ids[0][0],
                rider_id=fixture.rider_ids[0],
                client_incident_id=client_id,
                reason_code="OTHER",
            )
            for _ in range(2)
        )
    )
    assert incidents[0].incident_id == incidents[1].incident_id
    async with database_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(PickupIncident))
    assert count == 1


async def test_fresh_incident_requires_started_busy_current_ownership(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await assigned_fixture(database_session_factory)
    pickup_id = fixture.pickup_ids[0][0]
    with pytest.raises(PickupIncidentStateError):
        await open_pickup_incident(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_incident_id=new_uuid7(),
            reason_code="ADDRESS_NOT_FOUND",
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
    with pytest.raises(PickupIncidentStateError):
        await open_pickup_incident(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_incident_id=new_uuid7(),
            reason_code="ADDRESS_NOT_FOUND",
        )
    async with database_session_factory() as session, session.begin():
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert availability is not None
        availability.work_state = WORK_BUSY
    successor = await reassign(database_session_factory, fixture, assignment)
    assert successor.rider_id == fixture.rider_ids[1]
    with pytest.raises(PickupIncidentConflictError):
        await open_pickup_incident(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_incident_id=new_uuid7(),
            reason_code="ADDRESS_NOT_FOUND",
        )


async def test_prestart_reassignment_transfers_all_work_and_no_outbox(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, predecessor = await assigned_fixture(database_session_factory, pickup_count=3)
    old_time = utc_now().replace(microsecond=0)
    successor = await reassign_outstanding_work(
        database_session_factory,
        predecessor_assignment_id=predecessor.assignment_id,
        replacement_rider_id=fixture.rider_ids[1],
        manager_user_id=fixture.manager_id,
        client_reassignment_id=new_uuid7(),
        idempotency_expires_at=old_time + timedelta(days=1),
        now=old_time,
    )

    async with database_session_factory() as session:
        predecessor = await session.get(RiderAssignment, predecessor.assignment_id)
        old_availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        replacement = await session.get(RiderAvailability, fixture.rider_ids[1])
        old_items = list(
            await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == predecessor.assignment_id
                )
            )
        )
        successor_items = list(
            await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == successor.assignment_id
                )
            )
        )
        pickup_states = list(await session.scalars(select(PickupExecution.status)))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert predecessor is not None
    assert (predecessor.status, predecessor.completed_at, predecessor.superseded_at) == (
        ASSIGNMENT_SUPERSEDED,
        None,
        old_time,
    )
    assert old_availability is not None
    assert (old_availability.availability_intent, old_availability.work_state) == (
        INTENT_AVAILABLE,
        WORK_IDLE,
    )
    assert old_availability.version == 3
    assert replacement is not None
    assert (replacement.availability_intent, replacement.work_state, replacement.version) == (
        INTENT_AVAILABLE,
        WORK_RESERVED,
        2,
    )
    assert successor.status == ASSIGNMENT_ACTIVE
    assert successor.started_at is None
    assert successor.source == SOURCE_MANAGER_ASSIGNED
    assert successor.supersedes_assignment_id == predecessor.assignment_id
    assert all(
        item.released_at == old_time and item.release_reason_code == RELEASE_REASSIGNED
        for item in old_items
    )
    assert {item.pickup_execution_id for item in successor_items} == set(fixture.pickup_ids[0])
    assert all(
        item.released_at is None and item.release_reason_code is None for item in successor_items
    )
    assert set(pickup_states) == {PICKUP_ASSIGNED}
    assert outbox_count == 1


async def test_midroute_reassignment_retains_collected_and_resolves_incident_once(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, predecessor = await assigned_fixture(
        database_session_factory, pickup_count=4, started=True
    )
    first, second, third, fourth = fixture.pickup_ids[0]
    for pickup_id in (first, second):
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_COLLECTED,
        )
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=third,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_NOT_COLLECTED,
    )
    incident = await open_pickup_incident(
        database_session_factory,
        pickup_execution_id=third,
        rider_id=fixture.rider_ids[0],
        client_incident_id=new_uuid7(),
        reason_code="RIDER_UNABLE_TO_CONTINUE",
    )
    command_id = new_uuid7()
    reassigned_at = utc_now().replace(microsecond=0)
    successor = await reassign_outstanding_work(
        database_session_factory,
        predecessor_assignment_id=predecessor.assignment_id,
        replacement_rider_id=fixture.rider_ids[1],
        manager_user_id=fixture.manager_id,
        client_reassignment_id=command_id,
        incident_id=incident.incident_id,
        idempotency_expires_at=reassigned_at + timedelta(days=1),
        now=reassigned_at,
    )
    replay = await reassign_outstanding_work(
        database_session_factory,
        predecessor_assignment_id=predecessor.assignment_id,
        replacement_rider_id=fixture.rider_ids[1],
        manager_user_id=fixture.manager_id,
        client_reassignment_id=command_id,
        incident_id=incident.incident_id,
        idempotency_expires_at=reassigned_at + timedelta(days=2),
        now=reassigned_at + timedelta(hours=1),
    )
    incident_replay = await open_pickup_incident(
        database_session_factory,
        pickup_execution_id=third,
        rider_id=fixture.rider_ids[0],
        client_incident_id=incident.client_incident_id,
        reason_code=incident.reason_code,
    )

    async with database_session_factory() as session:
        predecessor = await session.get(RiderAssignment, predecessor.assignment_id)
        old_items = {
            item.pickup_execution_id: item
            for item in await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == predecessor.assignment_id
                )
            )
        }
        successor_ids = set(
            await session.scalars(
                select(RiderAssignmentItem.pickup_execution_id).where(
                    RiderAssignmentItem.assignment_id == successor.assignment_id
                )
            )
        )
        pickups = {
            pickup.pickup_execution_id: pickup
            for pickup in await session.scalars(
                select(PickupExecution).where(
                    PickupExecution.pickup_execution_id.in_(fixture.pickup_ids[0])
                )
            )
        }
        persisted_incident = await session.get(PickupIncident, incident.incident_id)
        old_availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        replacement = await session.get(RiderAvailability, fixture.rider_ids[1])
        attempts = list(await session.scalars(select(PickupAttempt)))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert replay.assignment_id == successor.assignment_id
    assert incident_replay.incident_id == incident.incident_id
    assert predecessor is not None and predecessor.status == ASSIGNMENT_SUPERSEDED
    assert predecessor.completed_at is None and predecessor.superseded_at == reassigned_at
    assert all(
        old_items[pickup_id].released_at is None
        and old_items[pickup_id].release_reason_code is None
        for pickup_id in (first, second)
    )
    assert all(
        old_items[pickup_id].released_at == reassigned_at
        and old_items[pickup_id].release_reason_code == RELEASE_REASSIGNED
        for pickup_id in (third, fourth)
    )
    assert successor_ids == {third, fourth}
    assert pickups[first].status == pickups[second].status == PICKUP_COLLECTED
    assert pickups[third].status == pickups[fourth].status == PICKUP_ASSIGNED
    assert len(attempts) == 3
    assert persisted_incident is not None
    assert (
        persisted_incident.status,
        persisted_incident.resolution_code,
        persisted_incident.resolved_by_user_id,
        persisted_incident.resolved_at,
    ) == (INCIDENT_RESOLVED, RESOLUTION_REASSIGNED, fixture.manager_id, reassigned_at)
    assert old_availability is not None
    assert (old_availability.work_state, old_availability.version) == (WORK_IDLE, 4)
    assert replacement is not None
    assert (replacement.work_state, replacement.version) == (WORK_RESERVED, 2)
    assert outbox_count == 1


async def test_reassignment_rejects_foreign_incident_same_rider_and_inconsistent_population(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=2, rider_count=4, pickups_per_group=2
    )
    first = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    second = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[1],
        rider_id=fixture.rider_ids[2],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        database_session_factory,
        assignment_id=second.assignment_id,
        rider_id=fixture.rider_ids[2],
    )
    foreign_incident = await open_pickup_incident(
        database_session_factory,
        pickup_execution_id=fixture.pickup_ids[1][0],
        rider_id=fixture.rider_ids[2],
        client_incident_id=new_uuid7(),
        reason_code="ACCESS_BLOCKED",
    )
    with pytest.raises(PickupIncidentConflictError):
        await reassign_outstanding_work(
            database_session_factory,
            predecessor_assignment_id=first.assignment_id,
            replacement_rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
            client_reassignment_id=new_uuid7(),
            incident_id=foreign_incident.incident_id,
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )
    with pytest.raises(ReassignmentRiderError):
        await reassign_outstanding_work(
            database_session_factory,
            predecessor_assignment_id=first.assignment_id,
            replacement_rider_id=fixture.rider_ids[0],
            manager_user_id=fixture.manager_id,
            client_reassignment_id=new_uuid7(),
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )
    async with database_session_factory() as session, session.begin():
        pickup = await session.get(PickupExecution, fixture.pickup_ids[0][0])
        assert pickup is not None
        pickup.status = "PENDING_ASSIGNMENT"
    with pytest.raises(ReassignmentStateError):
        await reassign(database_session_factory, fixture, first)


async def test_successor_lifecycle_continues_attempt_numbers_and_replays_after_completion(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, predecessor = await assigned_fixture(
        database_session_factory, pickup_count=2, started=True
    )
    outstanding, collected = fixture.pickup_ids[0]
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=outstanding,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_NOT_COLLECTED,
    )
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=collected,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_COLLECTED,
    )
    command_id = new_uuid7()
    successor = await reassign(
        database_session_factory, fixture, predecessor, command_id=command_id
    )
    await start_assignment(
        database_session_factory,
        assignment_id=successor.assignment_id,
        rider_id=fixture.rider_ids[1],
    )
    final_attempt = await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=outstanding,
        rider_id=fixture.rider_ids[1],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_COLLECTED,
    )
    replay = await reassign(database_session_factory, fixture, predecessor, command_id=command_id)
    with pytest.raises(IdempotencyKeyConflictError):
        await reassign_outstanding_work(
            database_session_factory,
            predecessor_assignment_id=predecessor.assignment_id,
            replacement_rider_id=fixture.rider_ids[2],
            manager_user_id=fixture.manager_id,
            client_reassignment_id=command_id,
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )

    async with database_session_factory() as session:
        successor = await session.get(RiderAssignment, successor.assignment_id)
        predecessor = await session.get(RiderAssignment, predecessor.assignment_id)
        replacement = await session.get(RiderAvailability, fixture.rider_ids[1])
        collected_item = await session.get(
            RiderAssignmentItem, (predecessor.assignment_id, collected)
        )
    assert final_attempt.attempt_number == 2
    assert replay.assignment_id == successor.assignment_id
    assert successor is not None and successor.status == ASSIGNMENT_COMPLETED
    assert replacement is not None
    assert (replacement.work_state, replacement.version) == (WORK_IDLE, 4)
    assert predecessor is not None and predecessor.status == ASSIGNMENT_SUPERSEDED
    assert collected_item is not None and collected_item.released_at is None


async def test_collection_and_reassignment_race_has_one_valid_winner(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, predecessor = await assigned_fixture(
        database_session_factory, pickup_count=1, started=True
    )
    pickup_id = fixture.pickup_ids[0][0]
    results = await asyncio.gather(
        record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_COLLECTED,
        ),
        reassign(database_session_factory, fixture, predecessor),
        return_exceptions=True,
    )
    async with database_session_factory() as session:
        persisted_predecessor = await session.get(RiderAssignment, predecessor.assignment_id)
        pickup = await session.get(PickupExecution, pickup_id)
        assignments = list(await session.scalars(select(RiderAssignment)))
        current_item = await session.scalar(
            select(RiderAssignmentItem).where(
                RiderAssignmentItem.pickup_execution_id == pickup_id,
                RiderAssignmentItem.released_at.is_(None),
            )
        )
    assert persisted_predecessor is not None and pickup is not None and current_item is not None
    if any(isinstance(result, PickupAttempt) for result in results):
        assert any(isinstance(result, ReassignmentConflictError) for result in results)
        assert persisted_predecessor.status == ASSIGNMENT_COMPLETED
        assert pickup.status == PICKUP_COLLECTED
        assert len(assignments) == 1
    else:
        assert any(isinstance(result, RiderAssignment) for result in results)
        assert any(isinstance(result, AssignmentNotStartableError) for result in results)
        assert persisted_predecessor.status == ASSIGNMENT_SUPERSEDED
        assert pickup.status == PICKUP_ASSIGNED
        assert len(assignments) == 2
        assert current_item.assignment_id != predecessor.assignment_id


async def test_competing_and_exact_reassignment_commands_serialize(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=2, rider_count=5, pickups_per_group=2
    )
    predecessor = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    different = await asyncio.gather(
        reassign(database_session_factory, fixture, predecessor, replacement_index=1),
        reassign(database_session_factory, fixture, predecessor, replacement_index=2),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RiderAssignment) for result in different) == 1
    assert sum(isinstance(result, ReassignmentConflictError) for result in different) == 1

    predecessor2 = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[1],
        rider_id=fixture.rider_ids[3],
        manager_user_id=fixture.manager_id,
    )
    command_id = new_uuid7()
    exact = await asyncio.gather(
        *(
            reassign(
                database_session_factory,
                fixture,
                predecessor2,
                replacement_index=4,
                command_id=command_id,
            )
            for _ in range(2)
        )
    )
    assert exact[0].assignment_id == exact[1].assignment_id
    async with database_session_factory() as session:
        successor_count = await session.scalar(
            select(func.count(RiderAssignment.assignment_id)).where(
                RiderAssignment.supersedes_assignment_id == predecessor2.assignment_id
            )
        )
    assert successor_count == 1


async def test_replacement_rider_race_allows_one_reservation(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=2, rider_count=2, pickups_per_group=1
    )
    predecessor = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    results = await asyncio.gather(
        reassign(database_session_factory, fixture, predecessor),
        assign_group_manually(
            database_session_factory,
            collection_group_id=fixture.group_ids[1],
            rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RiderAssignment) for result in results) == 1
    assert (
        sum(
            isinstance(result, (ReassignmentRiderError, RiderNotEligibleError))
            for result in results
        )
        == 1
    )
    async with database_session_factory() as session:
        replacement = await session.get(RiderAvailability, fixture.rider_ids[1])
    assert replacement is not None
    assert (replacement.work_state, replacement.version) == (WORK_RESERVED, 2)


async def test_late_failure_rolls_back_entire_incident_assisted_reassignment(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, predecessor = await assigned_fixture(
        database_session_factory, pickup_count=2, started=True
    )
    incident = await open_pickup_incident(
        database_session_factory,
        pickup_execution_id=fixture.pickup_ids[0][0],
        rider_id=fixture.rider_ids[0],
        client_incident_id=new_uuid7(),
        reason_code="RIDER_UNABLE_TO_CONTINUE",
    )
    old_availability_before = 3
    replacement_before = 1

    async def fail_completion(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected late reassignment failure")

    monkeypatch.setattr(operations_service, "complete_idempotency_record", fail_completion)
    with pytest.raises(RuntimeError, match="injected late reassignment failure"):
        await reassign_outstanding_work(
            database_session_factory,
            predecessor_assignment_id=predecessor.assignment_id,
            replacement_rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
            client_reassignment_id=new_uuid7(),
            incident_id=incident.incident_id,
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )

    async with database_session_factory() as session:
        predecessor = await session.get(RiderAssignment, predecessor.assignment_id)
        items = list(
            await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == predecessor.assignment_id
                )
            )
        )
        old_availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        replacement = await session.get(RiderAvailability, fixture.rider_ids[1])
        persisted_incident = await session.get(PickupIncident, incident.incident_id)
        successor_count = await session.scalar(
            select(func.count(RiderAssignment.assignment_id)).where(
                RiderAssignment.supersedes_assignment_id == predecessor.assignment_id
            )
        )
        idempotency_count = await session.scalar(
            select(func.count(IdempotencyRecord.idempotency_record_id)).where(
                IdempotencyRecord.scope == "pickup-reassignment"
            )
        )
    assert predecessor is not None
    assert (predecessor.status, predecessor.superseded_at) == (ASSIGNMENT_ACTIVE, None)
    assert all(item.released_at is None for item in items)
    assert successor_count == 0
    assert old_availability is not None
    assert (old_availability.work_state, old_availability.version) == (
        WORK_BUSY,
        old_availability_before,
    )
    assert replacement is not None
    assert (replacement.work_state, replacement.version) == (WORK_IDLE, replacement_before)
    assert persisted_incident is not None and persisted_incident.status == INCIDENT_OPEN
    assert idempotency_count == 0


async def test_reassignment_rechecks_replacement_profile_eligibility(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, predecessor = await assigned_fixture(database_session_factory)
    async with database_session_factory() as session, session.begin():
        profile = await session.get(RiderProfile, fixture.rider_ids[1])
        assert profile is not None
        profile.status = RIDER_SUSPENDED
    with pytest.raises(ReassignmentRiderError):
        await reassign(database_session_factory, fixture, predecessor)
