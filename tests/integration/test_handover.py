from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_rider_dispatch import DispatchFixture, create_fixture

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customers.service import GeoPoint, geography_point
from tirodhan.modules.dispatch.models import (
    RiderAssignment,
    RiderAssignmentItem,
    RiderAvailability,
)
from tirodhan.modules.dispatch.service import assign_group_manually
from tirodhan.modules.handovers import service as handover_service
from tirodhan.modules.handovers.models import HandoverEvent, HandoverEventItem
from tirodhan.modules.handovers.service import (
    HANDOVER_IDEMPOTENCY_SCOPE,
    HANDOVER_REJECTED,
    HANDOVER_VALIDATED,
    OUTSIDE_ALLOWED_RADIUS,
    WITHIN_ALLOWED_RADIUS,
    HandoverPickupNotCollectedError,
    HandoverPickupNotFoundError,
    HandoverRiderAttributionError,
    PickupAlreadyHandedOverError,
    ReceivingPointInactiveError,
    ReceivingPointNotFoundError,
    record_handover,
)
from tirodhan.modules.operations.service import reassign_outstanding_work
from tirodhan.modules.pickups.service import (
    ATTEMPT_COLLECTED,
    record_pickup_attempt,
    start_assignment,
)
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.receiving_points.models import ReceivingPoint
from tirodhan.modules.reliability.models import IdempotencyRecord, OutboxEvent
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DELHI = GeoPoint(latitude=28.6139, longitude=77.2090)


def future_expiry() -> datetime:
    return utc_now() + timedelta(days=1)


async def create_receiving_point(
    factory: async_sessionmaker[AsyncSession],
    *,
    location: GeoPoint = DELHI,
    radius_m: int = 250,
    status: str = "ACTIVE",
) -> ReceivingPoint:
    now = utc_now().replace(microsecond=0)
    async with factory() as session, session.begin():
        point = ReceivingPoint(
            receiving_point_id=new_uuid7(),
            official_name="Authorized test receiving point",
            official_identifier="RP-TEST",
            location=geography_point(location),
            allowed_radius_m=radius_m,
            status=status,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(point)
        await session.flush([point])
        return point


async def assign_and_collect(
    factory: async_sessionmaker[AsyncSession],
    *,
    pickup_count: int,
    collect_indexes: tuple[int, ...] | None = None,
    rider_count: int = 2,
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
    assignment = await start_assignment(
        factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    indexes = collect_indexes if collect_indexes is not None else tuple(range(pickup_count))
    for index in indexes:
        await record_pickup_attempt(
            factory,
            pickup_execution_id=fixture.pickup_ids[0][index],
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_COLLECTED,
        )
    return fixture, assignment


async def record(
    factory: async_sessionmaker[AsyncSession],
    fixture: DispatchFixture,
    point: ReceivingPoint,
    pickup_ids: tuple[UUID, ...],
    *,
    client_id: UUID | None = None,
    rider_id: UUID | None = None,
    observed_location: GeoPoint = DELHI,
) -> HandoverEvent:
    return await record_handover(
        factory,
        client_handover_id=client_id or new_uuid7(),
        rider_id=rider_id or fixture.rider_ids[0],
        receiving_point_id=point.receiving_point_id,
        pickup_execution_ids=pickup_ids,
        observed_location=observed_location,
        idempotency_expires_at=future_expiry(),
    )


@pytest.mark.parametrize("pickup_count", [1, 3])
async def test_validated_handover_supports_single_and_multi_pickup_completed_assignments(
    database_session_factory: async_sessionmaker[AsyncSession],
    pickup_count: int,
) -> None:
    fixture, assignment = await assign_and_collect(
        database_session_factory, pickup_count=pickup_count
    )
    point = await create_receiving_point(database_session_factory)
    pickup_ids = fixture.pickup_ids[0]

    async with database_session_factory() as session:
        outbox_before = await session.scalar(select(func.count()).select_from(OutboxEvent))
        requests_before = list(
            await session.scalars(
                select(CollectionRequest)
                .join(
                    PickupExecution,
                    PickupExecution.request_id == CollectionRequest.request_id,
                )
                .where(PickupExecution.pickup_execution_id.in_(pickup_ids))
                .order_by(CollectionRequest.request_id)
            )
        )

    event = await record(database_session_factory, fixture, point, pickup_ids)

    async with database_session_factory() as session:
        items = list(
            await session.scalars(
                select(HandoverEventItem)
                .where(HandoverEventItem.handover_event_id == event.handover_event_id)
                .order_by(HandoverEventItem.pickup_execution_id)
            )
        )
        persisted_assignment = await session.get(RiderAssignment, assignment.assignment_id)
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        pickups = list(
            await session.scalars(
                select(PickupExecution)
                .where(PickupExecution.pickup_execution_id.in_(pickup_ids))
                .order_by(PickupExecution.pickup_execution_id)
            )
        )
        requests_after = list(
            await session.scalars(
                select(CollectionRequest)
                .join(
                    PickupExecution,
                    PickupExecution.request_id == CollectionRequest.request_id,
                )
                .where(PickupExecution.pickup_execution_id.in_(pickup_ids))
                .order_by(CollectionRequest.request_id)
            )
        )
        outbox_after = await session.scalar(select(func.count()).select_from(OutboxEvent))

    assert (event.status, event.validation_code) == (
        HANDOVER_VALIDATED,
        WITHIN_ALLOWED_RADIUS,
    )
    assert event.distance_m == pytest.approx(0.0)
    assert event.occurred_at == event.created_at == event.evaluated_at
    assert len(items) == pickup_count
    assert all(item.status == HANDOVER_VALIDATED for item in items)
    assert all(item.created_at == event.created_at for item in items)
    assert all(item.evaluated_at == event.evaluated_at for item in items)
    assert persisted_assignment is not None and persisted_assignment.status == "COMPLETED"
    assert availability is not None and availability.work_state == "IDLE"
    assert all(pickup.status == "COLLECTED" for pickup in pickups)
    assert (
        [request.status for request in requests_before]
        == [request.status for request in requests_after]
        == ["PLANNED"] * pickup_count
    )
    assert outbox_after == outbox_before


async def test_validated_handover_allows_collected_pickup_on_active_assignment(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await assign_and_collect(
        database_session_factory,
        pickup_count=2,
        collect_indexes=(0,),
    )
    point = await create_receiving_point(database_session_factory)

    event = await record(
        database_session_factory,
        fixture,
        point,
        (fixture.pickup_ids[0][0],),
    )

    async with database_session_factory() as session:
        persisted = await session.get(RiderAssignment, assignment.assignment_id)
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
    assert event.status == HANDOVER_VALIDATED
    assert persisted is not None and persisted.status == "ACTIVE"
    assert availability is not None and availability.work_state == "BUSY"


async def test_validated_handover_allows_collected_pickup_on_superseded_assignment(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, predecessor = await assign_and_collect(
        database_session_factory,
        pickup_count=2,
        collect_indexes=(0,),
    )
    await reassign_outstanding_work(
        database_session_factory,
        predecessor_assignment_id=predecessor.assignment_id,
        replacement_rider_id=fixture.rider_ids[1],
        manager_user_id=fixture.manager_id,
        client_reassignment_id=new_uuid7(),
        idempotency_expires_at=future_expiry(),
    )
    point = await create_receiving_point(database_session_factory)

    event = await record(
        database_session_factory,
        fixture,
        point,
        (fixture.pickup_ids[0][0],),
    )

    async with database_session_factory() as session:
        persisted = await session.get(RiderAssignment, predecessor.assignment_id)
    assert event.status == HANDOVER_VALIDATED
    assert persisted is not None and persisted.status == "SUPERSEDED"


async def test_one_handover_may_cover_pickups_from_multiple_assignments_for_same_rider(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(
        database_session_factory,
        group_count=2,
        rider_count=2,
        pickups_per_group=1,
    )
    for group_id, pickup_ids in zip(fixture.group_ids, fixture.pickup_ids, strict=True):
        assignment = await assign_group_manually(
            database_session_factory,
            collection_group_id=group_id,
            rider_id=fixture.rider_ids[0],
            manager_user_id=fixture.manager_id,
        )
        await start_assignment(
            database_session_factory,
            assignment_id=assignment.assignment_id,
            rider_id=fixture.rider_ids[0],
        )
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_ids[0],
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_COLLECTED,
        )
    point = await create_receiving_point(database_session_factory)
    all_pickups = tuple(pickups[0] for pickups in fixture.pickup_ids)

    event = await record(database_session_factory, fixture, point, all_pickups)

    async with database_session_factory() as session:
        assignment_ids = set(
            await session.scalars(
                select(RiderAssignmentItem.assignment_id).where(
                    RiderAssignmentItem.pickup_execution_id.in_(all_pickups)
                )
            )
        )
        item_count = await session.scalar(
            select(func.count())
            .select_from(HandoverEventItem)
            .where(HandoverEventItem.handover_event_id == event.handover_event_id)
        )
    assert event.status == HANDOVER_VALIDATED
    assert len(assignment_ids) == 2
    assert item_count == 2


async def test_postgis_boundary_is_inclusive_and_database_authoritative(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=2)
    point = await create_receiving_point(database_session_factory, radius_m=500)
    async with database_session_factory() as session:
        boundary = (
            await session.execute(
                text(
                    "SELECT "
                    "ST_Y(ST_Project(location, allowed_radius_m - 1, radians(90))::geometry), "
                    "ST_X(ST_Project(location, allowed_radius_m - 1, radians(90))::geometry), "
                    "ST_DWithin(location, "
                    "ST_Project(location, allowed_radius_m - 1, radians(90)), allowed_radius_m), "
                    "ST_Distance(location, "
                    "ST_Project(location, allowed_radius_m - 1, radians(90))), "
                    "ST_Y(ST_Project(location, allowed_radius_m + 1, radians(90))::geometry), "
                    "ST_X(ST_Project(location, allowed_radius_m + 1, radians(90))::geometry), "
                    "ST_DWithin(location, "
                    "ST_Project(location, allowed_radius_m + 1, radians(90)), allowed_radius_m), "
                    "ST_Distance(location, "
                    "ST_Project(location, allowed_radius_m + 1, radians(90))) "
                    "FROM receiving_point WHERE receiving_point_id = :point_id"
                ),
                {"point_id": point.receiving_point_id},
            )
        ).one()
    inside_observed = GeoPoint(latitude=float(boundary[0]), longitude=float(boundary[1]))
    outside_observed = GeoPoint(latitude=float(boundary[4]), longitude=float(boundary[5]))

    inside_event = await record(
        database_session_factory,
        fixture,
        point,
        (fixture.pickup_ids[0][0],),
        observed_location=inside_observed,
    )
    outside_event = await record(
        database_session_factory,
        fixture,
        point,
        (fixture.pickup_ids[0][1],),
        observed_location=outside_observed,
    )

    assert boundary[2] is True
    assert boundary[6] is False
    assert float(boundary[3]) == pytest.approx(499.0, abs=1e-6)
    assert float(boundary[7]) == pytest.approx(501.0, abs=1e-6)
    assert inside_event.status == HANDOVER_VALIDATED
    assert inside_event.validation_code == WITHIN_ALLOWED_RADIUS
    assert inside_event.distance_m == pytest.approx(499.0, abs=1e-5)
    assert outside_event.status == HANDOVER_REJECTED
    assert outside_event.validation_code == OUTSIDE_ALLOWED_RADIUS
    assert outside_event.distance_m == pytest.approx(501.0, abs=1e-5)


async def test_outside_radius_is_durable_rejection_and_later_command_can_validate(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    point = await create_receiving_point(database_session_factory, radius_m=50)
    pickup_ids = fixture.pickup_ids[0]

    rejected = await record(
        database_session_factory,
        fixture,
        point,
        pickup_ids,
        observed_location=GeoPoint(latitude=28.7, longitude=77.3),
    )
    validated = await record(database_session_factory, fixture, point, pickup_ids)

    async with database_session_factory() as session:
        events = list(
            await session.scalars(select(HandoverEvent).order_by(HandoverEvent.created_at))
        )
        rejected_items = list(
            await session.scalars(
                select(HandoverEventItem).where(
                    HandoverEventItem.handover_event_id == rejected.handover_event_id
                )
            )
        )
    assert (rejected.status, rejected.validation_code) == (
        HANDOVER_REJECTED,
        OUTSIDE_ALLOWED_RADIUS,
    )
    assert rejected.distance_m > rejected.allowed_radius_m_snapshot
    assert all(item.status == HANDOVER_REJECTED for item in rejected_items)
    assert validated.status == HANDOVER_VALIDATED
    assert len(events) == 2


async def test_fresh_handover_rejects_point_pickup_and_rider_state_errors(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(
        database_session_factory,
        pickup_count=2,
        collect_indexes=(0,),
    )
    active_point = await create_receiving_point(database_session_factory)
    inactive_point = await create_receiving_point(database_session_factory, status="INACTIVE")
    collected, assigned = fixture.pickup_ids[0]

    with pytest.raises(ReceivingPointNotFoundError):
        await record_handover(
            database_session_factory,
            client_handover_id=new_uuid7(),
            rider_id=fixture.rider_ids[0],
            receiving_point_id=new_uuid7(),
            pickup_execution_ids=(collected,),
            observed_location=DELHI,
            idempotency_expires_at=future_expiry(),
        )
    with pytest.raises(ReceivingPointInactiveError):
        await record(database_session_factory, fixture, inactive_point, (collected,))
    with pytest.raises(HandoverPickupNotFoundError):
        await record(database_session_factory, fixture, active_point, (new_uuid7(),))
    with pytest.raises(HandoverPickupNotCollectedError):
        await record(database_session_factory, fixture, active_point, (assigned,))
    with pytest.raises(HandoverPickupNotCollectedError):
        await record(database_session_factory, fixture, active_point, (collected, assigned))
    with pytest.raises(HandoverRiderAttributionError):
        await record(
            database_session_factory,
            fixture,
            active_point,
            (collected,),
            rider_id=fixture.rider_ids[1],
        )

    await record(database_session_factory, fixture, active_point, (collected,))
    with pytest.raises(PickupAlreadyHandedOverError):
        await record(database_session_factory, fixture, active_point, (collected,))


async def test_exact_replay_ignores_later_master_and_assignment_history_changes(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, predecessor = await assign_and_collect(
        database_session_factory,
        pickup_count=2,
        collect_indexes=(0,),
    )
    point = await create_receiving_point(database_session_factory)
    pickup_id = fixture.pickup_ids[0][0]
    command_id = new_uuid7()
    first = await record(
        database_session_factory,
        fixture,
        point,
        (pickup_id,),
        client_id=command_id,
    )
    await reassign_outstanding_work(
        database_session_factory,
        predecessor_assignment_id=predecessor.assignment_id,
        replacement_rider_id=fixture.rider_ids[1],
        manager_user_id=fixture.manager_id,
        client_reassignment_id=new_uuid7(),
        idempotency_expires_at=future_expiry(),
    )
    async with database_session_factory() as session, session.begin():
        persisted_point = await session.get(ReceivingPoint, point.receiving_point_id)
        assert persisted_point is not None
        persisted_point.status = "INACTIVE"
        persisted_point.location = WKTElement("POINT(80 30)", srid=4326)
        persisted_point.allowed_radius_m = 1
        persisted_point.version += 1

    replay = await record(
        database_session_factory,
        fixture,
        point,
        (pickup_id,),
        client_id=command_id,
    )

    async with database_session_factory() as session:
        event_count = await session.scalar(select(func.count()).select_from(HandoverEvent))
        predecessor = await session.get(RiderAssignment, predecessor.assignment_id)
    assert replay.handover_event_id == first.handover_event_id
    assert replay.status == HANDOVER_VALIDATED
    assert replay.allowed_radius_m_snapshot == 250
    assert event_count == 1
    assert predecessor is not None and predecessor.status == "SUPERSEDED"


async def test_reusing_client_handover_id_with_any_changed_fingerprint_fact_conflicts(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=2)
    point = await create_receiving_point(database_session_factory)
    other_point = await create_receiving_point(database_session_factory)
    command_id = new_uuid7()
    first_pickup, second_pickup = fixture.pickup_ids[0]
    await record(
        database_session_factory,
        fixture,
        point,
        (first_pickup,),
        client_id=command_id,
    )

    changed_commands = (
        {
            "rider_id": fixture.rider_ids[1],
            "receiving_point_id": point.receiving_point_id,
            "pickup_execution_ids": (first_pickup,),
            "observed_location": DELHI,
        },
        {
            "rider_id": fixture.rider_ids[0],
            "receiving_point_id": other_point.receiving_point_id,
            "pickup_execution_ids": (first_pickup,),
            "observed_location": DELHI,
        },
        {
            "rider_id": fixture.rider_ids[0],
            "receiving_point_id": point.receiving_point_id,
            "pickup_execution_ids": (second_pickup,),
            "observed_location": DELHI,
        },
        {
            "rider_id": fixture.rider_ids[0],
            "receiving_point_id": point.receiving_point_id,
            "pickup_execution_ids": (first_pickup,),
            "observed_location": GeoPoint(latitude=28.614, longitude=77.209),
        },
    )
    for changed in changed_commands:
        with pytest.raises(IdempotencyKeyConflictError):
            await record_handover(
                database_session_factory,
                client_handover_id=command_id,
                idempotency_expires_at=future_expiry(),
                **changed,  # type: ignore[arg-type]
            )


async def test_rejected_event_does_not_establish_a_validated_pickup_result(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    point = await create_receiving_point(database_session_factory, radius_m=10)
    pickup_ids = fixture.pickup_ids[0]
    first = await record(
        database_session_factory,
        fixture,
        point,
        pickup_ids,
        observed_location=GeoPoint(latitude=28.7, longitude=77.3),
    )
    second = await record(
        database_session_factory,
        fixture,
        point,
        pickup_ids,
        observed_location=GeoPoint(latitude=28.7, longitude=77.3),
    )
    assert first.handover_event_id != second.handover_event_id
    assert first.status == second.status == HANDOVER_REJECTED


async def test_exact_concurrent_handover_commands_converge_on_one_event(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    point = await create_receiving_point(database_session_factory)
    command_id = new_uuid7()

    results = await asyncio.gather(
        *(
            record(
                database_session_factory,
                fixture,
                point,
                fixture.pickup_ids[0],
                client_id=command_id,
            )
            for _ in range(2)
        )
    )

    async with database_session_factory() as session:
        event_count = await session.scalar(select(func.count()).select_from(HandoverEvent))
        item_count = await session.scalar(select(func.count()).select_from(HandoverEventItem))
        idempotency_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(IdempotencyRecord.scope == HANDOVER_IDEMPOTENCY_SCOPE)
        )
    assert results[0].handover_event_id == results[1].handover_event_id
    assert (event_count, item_count, idempotency_count) == (1, 1, 1)


async def test_competing_valid_handovers_for_same_pickup_have_one_winner(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    point = await create_receiving_point(database_session_factory)

    results = await asyncio.gather(
        *(
            record(
                database_session_factory,
                fixture,
                point,
                fixture.pickup_ids[0],
                client_id=new_uuid7(),
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )

    async with database_session_factory() as session:
        event_count = await session.scalar(select(func.count()).select_from(HandoverEvent))
        item_count = await session.scalar(select(func.count()).select_from(HandoverEventItem))
        idempotency_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(IdempotencyRecord.scope == HANDOVER_IDEMPOTENCY_SCOPE)
        )
    assert sum(isinstance(result, HandoverEvent) for result in results) == 1
    assert sum(isinstance(result, PickupAlreadyHandedOverError) for result in results) == 1
    assert (event_count, item_count, idempotency_count) == (1, 1, 1)


async def test_handover_and_collection_race_has_a_valid_serialized_outcome(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(
        database_session_factory,
        group_count=1,
        rider_count=2,
        pickups_per_group=1,
    )
    assignment = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    point = await create_receiving_point(database_session_factory)
    command_id = new_uuid7()

    collection_result, handover_result = await asyncio.gather(
        record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=fixture.pickup_ids[0][0],
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome=ATTEMPT_COLLECTED,
        ),
        record(
            database_session_factory,
            fixture,
            point,
            fixture.pickup_ids[0],
            client_id=command_id,
        ),
        return_exceptions=True,
    )
    assert not isinstance(collection_result, Exception)
    if isinstance(handover_result, HandoverPickupNotCollectedError):
        event = await record(
            database_session_factory,
            fixture,
            point,
            fixture.pickup_ids[0],
            client_id=command_id,
        )
    else:
        assert isinstance(handover_result, HandoverEvent)
        event = handover_result

    async with database_session_factory() as session:
        pickup = await session.get(PickupExecution, fixture.pickup_ids[0][0])
        event_count = await session.scalar(select(func.count()).select_from(HandoverEvent))
        idempotency_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(IdempotencyRecord.scope == HANDOVER_IDEMPOTENCY_SCOPE)
        )
    assert event.status == HANDOVER_VALIDATED
    assert pickup is not None and pickup.status == "COLLECTED"
    assert event_count == 1
    assert idempotency_count == 1


async def test_late_failure_rolls_back_handover_items_and_idempotency(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=2)
    point = await create_receiving_point(database_session_factory)

    async def fail_completion(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected late handover failure")

    monkeypatch.setattr(handover_service, "complete_idempotency_record", fail_completion)
    with pytest.raises(RuntimeError, match="injected late handover failure"):
        await record(database_session_factory, fixture, point, fixture.pickup_ids[0])

    async with database_session_factory() as session:
        event_count = await session.scalar(select(func.count()).select_from(HandoverEvent))
        item_count = await session.scalar(select(func.count()).select_from(HandoverEventItem))
        idempotency_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(IdempotencyRecord.scope == HANDOVER_IDEMPOTENCY_SCOPE)
        )
    assert (event_count, item_count, idempotency_count) == (0, 0, 0)


async def test_handover_reliability_metadata_contains_hash_and_internal_result_only(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    point = await create_receiving_point(database_session_factory)
    event = await record(database_session_factory, fixture, point, fixture.pickup_ids[0])

    async with database_session_factory() as session:
        record_row = await session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.scope == HANDOVER_IDEMPOTENCY_SCOPE)
        )
    assert record_row is not None
    assert record_row.result_resource_id == event.handover_event_id
    assert len(record_row.request_fingerprint) == 32
    assert record_row.result_status_code is None
    assert not hasattr(record_row, "request_body")


async def test_database_checks_reject_invalid_handover_rows(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    point = await create_receiving_point(database_session_factory)
    event = await record(database_session_factory, fixture, point, fixture.pickup_ids[0])

    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(ReceivingPoint)
                .where(ReceivingPoint.receiving_point_id == point.receiving_point_id)
                .values(allowed_radius_m=0)
            )
    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(HandoverEvent)
                .where(HandoverEvent.handover_event_id == event.handover_event_id)
                .values(validation_code=OUTSIDE_ALLOWED_RADIUS)
            )
    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(HandoverEvent)
                .where(HandoverEvent.handover_event_id == event.handover_event_id)
                .values(distance_m=-1)
            )
    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(HandoverEventItem)
                .where(HandoverEventItem.handover_event_id == event.handover_event_id)
                .values(status="UNKNOWN")
            )
