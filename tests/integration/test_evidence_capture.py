from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_handover import assign_and_collect, create_receiving_point
from test_handover import record as record_handover
from test_rider_dispatch import DispatchFixture, create_fixture

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customers.service import GeoPoint
from tirodhan.modules.dispatch.models import (
    RiderAssignment,
    RiderAvailability,
)
from tirodhan.modules.dispatch.service import (
    INTENT_OFFLINE,
    assign_group_manually,
    set_rider_availability_intent,
)
from tirodhan.modules.evidence import service as evidence_service
from tirodhan.modules.evidence.models import (
    EvidenceCapture,
    HandoverEvidenceLink,
    PickupEvidenceLink,
)
from tirodhan.modules.evidence.service import (
    EVIDENCE_CAPTURE_IDEMPOTENCY_SCOPE,
    TARGET_HANDOVER,
    TARGET_PICKUP,
    EvidenceHandoverAttributionError,
    EvidenceHandoverNotFoundError,
    EvidencePickupAttributionError,
    EvidencePickupNotCollectedError,
    EvidencePickupNotFoundError,
    record_evidence_capture,
)
from tirodhan.modules.handovers.models import HandoverEvent
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

CAPTURED_AT = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)


def future_expiry() -> datetime:
    return utc_now() + timedelta(days=1)


async def capture(
    factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: UUID,
    target_kind: str,
    target_id: UUID,
    client_id: UUID | None = None,
    captured_at: datetime = CAPTURED_AT,
    now: datetime | None = None,
) -> EvidenceCapture:
    return await record_evidence_capture(
        factory,
        client_capture_id=client_id or new_uuid7(),
        captured_by_user_id=actor_id,
        target_kind=target_kind,
        target_id=target_id,
        captured_at=captured_at,
        idempotency_expires_at=future_expiry(),
        now=now,
    )


async def pickup_state(
    factory: async_sessionmaker[AsyncSession],
    *,
    pickup_id: UUID,
    assignment_id: UUID,
    rider_id: UUID,
) -> tuple[object, ...]:
    async with factory() as session:
        pickup = await session.get(PickupExecution, pickup_id)
        assignment = await session.get(RiderAssignment, assignment_id)
        availability = await session.get(RiderAvailability, rider_id)
        request = await session.scalar(
            select(CollectionRequest)
            .join(PickupExecution, PickupExecution.request_id == CollectionRequest.request_id)
            .where(PickupExecution.pickup_execution_id == pickup_id)
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert pickup is not None and assignment is not None
    assert availability is not None and request is not None
    return (
        pickup.status,
        pickup.collected_at,
        pickup.completed_at,
        pickup.updated_at,
        assignment.status,
        assignment.started_at,
        assignment.completed_at,
        assignment.superseded_at,
        availability.availability_intent,
        availability.work_state,
        availability.version,
        request.status,
        request.completed_at,
        outbox_count,
    )


@pytest.mark.parametrize("assignment_state", ["ACTIVE", "COMPLETED", "SUPERSEDED"])
async def test_collected_pickup_evidence_accepts_historical_assignment_states_without_mutation(
    database_session_factory: async_sessionmaker[AsyncSession],
    assignment_state: str,
) -> None:
    if assignment_state == "COMPLETED":
        fixture, assignment = await assign_and_collect(
            database_session_factory,
            pickup_count=1,
        )
    else:
        fixture, assignment = await assign_and_collect(
            database_session_factory,
            pickup_count=2,
            collect_indexes=(0,),
        )
    pickup_id = fixture.pickup_ids[0][0]
    if assignment_state == "SUPERSEDED":
        await reassign_outstanding_work(
            database_session_factory,
            predecessor_assignment_id=assignment.assignment_id,
            replacement_rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
            client_reassignment_id=new_uuid7(),
            idempotency_expires_at=future_expiry(),
        )

    before = await pickup_state(
        database_session_factory,
        pickup_id=pickup_id,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    evidence = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind=TARGET_PICKUP,
        target_id=pickup_id,
    )
    after = await pickup_state(
        database_session_factory,
        pickup_id=pickup_id,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )

    async with database_session_factory() as session:
        pickup_link = await session.get(
            PickupEvidenceLink,
            (pickup_id, evidence.evidence_capture_id),
        )
        handover_link_count = await session.scalar(
            select(func.count())
            .select_from(HandoverEvidenceLink)
            .where(HandoverEvidenceLink.evidence_capture_id == evidence.evidence_capture_id)
        )
    assert before == after
    assert before[0] == "COLLECTED"
    assert before[4] == assignment_state
    assert before[11] == "PLANNED"
    assert pickup_link is not None
    assert handover_link_count == 0


async def test_multiple_distinct_captures_may_link_to_same_pickup(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    pickup_id = fixture.pickup_ids[0][0]

    captures = [
        await capture(
            database_session_factory,
            actor_id=fixture.rider_ids[0],
            target_kind=TARGET_PICKUP,
            target_id=pickup_id,
            captured_at=CAPTURED_AT + timedelta(seconds=index),
        )
        for index in range(3)
    ]

    async with database_session_factory() as session:
        link_count = await session.scalar(
            select(func.count())
            .select_from(PickupEvidenceLink)
            .where(PickupEvidenceLink.pickup_execution_id == pickup_id)
        )
    assert len({item.evidence_capture_id for item in captures}) == 3
    assert link_count == 3


async def test_fresh_pickup_evidence_rejects_missing_uncollected_and_wrong_rider_targets(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pending_fixture = await create_fixture(
        database_session_factory,
        group_count=1,
        rider_count=2,
        pickups_per_group=1,
    )
    pending_id = pending_fixture.pickup_ids[0][0]
    with pytest.raises(EvidencePickupNotCollectedError):
        await capture(
            database_session_factory,
            actor_id=pending_fixture.rider_ids[0],
            target_kind=TARGET_PICKUP,
            target_id=pending_id,
        )

    assignment = await assign_group_manually(
        database_session_factory,
        collection_group_id=pending_fixture.group_ids[0],
        rider_id=pending_fixture.rider_ids[0],
        manager_user_id=pending_fixture.manager_id,
    )
    await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=pending_fixture.rider_ids[0],
    )
    with pytest.raises(EvidencePickupNotCollectedError):
        await capture(
            database_session_factory,
            actor_id=pending_fixture.rider_ids[0],
            target_kind=TARGET_PICKUP,
            target_id=pending_id,
        )
    with pytest.raises(EvidencePickupNotFoundError):
        await capture(
            database_session_factory,
            actor_id=pending_fixture.rider_ids[0],
            target_kind=TARGET_PICKUP,
            target_id=new_uuid7(),
        )

    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pending_id,
        rider_id=pending_fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_COLLECTED,
    )
    with pytest.raises(EvidencePickupAttributionError):
        await capture(
            database_session_factory,
            actor_id=pending_fixture.rider_ids[1],
            target_kind=TARGET_PICKUP,
            target_id=pending_id,
        )


async def test_pickup_evidence_rejects_before_collection_then_new_command_succeeds(
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
    pickup_id = fixture.pickup_ids[0][0]
    with pytest.raises(EvidencePickupNotCollectedError):
        await capture(
            database_session_factory,
            actor_id=fixture.rider_ids[0],
            target_kind=TARGET_PICKUP,
            target_id=pickup_id,
        )
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_COLLECTED,
    )
    evidence = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind=TARGET_PICKUP,
        target_id=pickup_id,
    )
    assert evidence.evidence_capture_id is not None


async def create_handover_fixture(
    factory: async_sessionmaker[AsyncSession],
    *,
    validated: bool,
) -> tuple[DispatchFixture, ReceivingPoint, HandoverEvent]:
    fixture, _assignment = await assign_and_collect(factory, pickup_count=1)
    point = await create_receiving_point(factory, radius_m=50)
    event = await record_handover(
        factory,
        fixture,
        point,
        fixture.pickup_ids[0],
        observed_location=(
            GeoPoint(latitude=28.6139, longitude=77.2090)
            if validated
            else GeoPoint(latitude=28.7, longitude=77.3)
        ),
    )
    return fixture, point, event


@pytest.mark.parametrize(
    ("validated", "expected_status"),
    [(True, "VALIDATED"), (False, "REJECTED")],
)
async def test_handover_evidence_accepts_validated_and_rejected_events_without_mutation(
    database_session_factory: async_sessionmaker[AsyncSession],
    validated: bool,
    expected_status: str,
) -> None:
    fixture, _point, event = await create_handover_fixture(
        database_session_factory,
        validated=validated,
    )
    async with database_session_factory() as session:
        before = await session.get(HandoverEvent, event.handover_event_id)
        outbox_before = await session.scalar(select(func.count()).select_from(OutboxEvent))
        assert before is not None
        event_state = (
            before.status,
            before.validation_code,
            bytes(before.observed_location.data),
            bytes(before.receiving_point_location_snapshot.data),
            before.allowed_radius_m_snapshot,
            before.distance_m,
            before.evaluated_at,
        )

    evidence = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind=TARGET_HANDOVER,
        target_id=event.handover_event_id,
    )

    async with database_session_factory() as session:
        after = await session.get(HandoverEvent, event.handover_event_id)
        handover_link = await session.get(
            HandoverEvidenceLink,
            (event.handover_event_id, evidence.evidence_capture_id),
        )
        pickup_link_count = await session.scalar(
            select(func.count())
            .select_from(PickupEvidenceLink)
            .where(PickupEvidenceLink.evidence_capture_id == evidence.evidence_capture_id)
        )
        outbox_after = await session.scalar(select(func.count()).select_from(OutboxEvent))
        assert after is not None
        after_state = (
            after.status,
            after.validation_code,
            bytes(after.observed_location.data),
            bytes(after.receiving_point_location_snapshot.data),
            after.allowed_radius_m_snapshot,
            after.distance_m,
            after.evaluated_at,
        )
    assert event_state == after_state
    assert after.status == expected_status
    assert handover_link is not None
    assert pickup_link_count == 0
    assert outbox_after == outbox_before


async def test_fresh_handover_evidence_rejects_missing_and_wrong_rider_targets(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _point, event = await create_handover_fixture(
        database_session_factory,
        validated=True,
    )
    with pytest.raises(EvidenceHandoverNotFoundError):
        await capture(
            database_session_factory,
            actor_id=fixture.rider_ids[0],
            target_kind=TARGET_HANDOVER,
            target_id=new_uuid7(),
        )
    with pytest.raises(EvidenceHandoverAttributionError):
        await capture(
            database_session_factory,
            actor_id=fixture.rider_ids[1],
            target_kind=TARGET_HANDOVER,
            target_id=event.handover_event_id,
        )


async def test_multiple_distinct_captures_may_link_to_same_handover(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _point, event = await create_handover_fixture(
        database_session_factory,
        validated=True,
    )
    captures = [
        await capture(
            database_session_factory,
            actor_id=fixture.rider_ids[0],
            target_kind=TARGET_HANDOVER,
            target_id=event.handover_event_id,
            captured_at=CAPTURED_AT + timedelta(seconds=index),
        )
        for index in range(3)
    ]

    async with database_session_factory() as session:
        link_count = await session.scalar(
            select(func.count())
            .select_from(HandoverEvidenceLink)
            .where(HandoverEvidenceLink.handover_event_id == event.handover_event_id)
        )
    assert len({item.evidence_capture_id for item in captures}) == 3
    assert link_count == 3


async def test_capture_timestamps_normalize_and_exact_replay_preserves_original_result(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    pickup_id = fixture.pickup_ids[0][0]
    client_id = new_uuid7()
    claimed = datetime(2026, 9, 28, 18, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    normalized = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
    registered = datetime(2026, 9, 28, 13, 5, tzinfo=timezone.utc)
    first = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind=TARGET_PICKUP,
        target_id=pickup_id,
        client_id=client_id,
        captured_at=claimed,
        now=registered,
    )
    replay = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind=TARGET_PICKUP,
        target_id=pickup_id,
        client_id=client_id,
        captured_at=normalized,
        now=registered + timedelta(hours=1),
    )

    assert first.evidence_capture_id == replay.evidence_capture_id
    assert first.captured_at == replay.captured_at == normalized
    assert first.created_at == replay.created_at == registered
    assert first.captured_at != first.created_at
    with pytest.raises(IdempotencyKeyConflictError):
        await capture(
            database_session_factory,
            actor_id=fixture.rider_ids[0],
            target_kind=TARGET_PICKUP,
            target_id=pickup_id,
            client_id=client_id,
            captured_at=normalized + timedelta(seconds=1),
        )


async def test_same_client_capture_id_conflicts_when_any_fingerprint_fact_changes(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=2)
    first_pickup, second_pickup = fixture.pickup_ids[0]
    client_id = new_uuid7()
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind=TARGET_PICKUP,
        target_id=first_pickup,
        client_id=client_id,
    )
    changes = (
        (fixture.rider_ids[1], TARGET_PICKUP, first_pickup, CAPTURED_AT),
        (fixture.rider_ids[0], TARGET_HANDOVER, first_pickup, CAPTURED_AT),
        (fixture.rider_ids[0], TARGET_PICKUP, second_pickup, CAPTURED_AT),
        (fixture.rider_ids[0], TARGET_PICKUP, first_pickup, CAPTURED_AT + timedelta(seconds=1)),
    )
    for actor_id, target_kind, target_id, captured_at in changes:
        with pytest.raises(IdempotencyKeyConflictError):
            await capture(
                database_session_factory,
                actor_id=actor_id,
                target_kind=target_kind,
                target_id=target_id,
                client_id=client_id,
                captured_at=captured_at,
            )


async def test_pickup_capture_replays_after_assignment_and_rider_state_change(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, predecessor = await assign_and_collect(
        database_session_factory,
        pickup_count=2,
        collect_indexes=(0,),
    )
    pickup_id = fixture.pickup_ids[0][0]
    client_id = new_uuid7()
    first = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind=TARGET_PICKUP,
        target_id=pickup_id,
        client_id=client_id,
    )
    await reassign_outstanding_work(
        database_session_factory,
        predecessor_assignment_id=predecessor.assignment_id,
        replacement_rider_id=fixture.rider_ids[1],
        manager_user_id=fixture.manager_id,
        client_reassignment_id=new_uuid7(),
        idempotency_expires_at=future_expiry(),
    )
    async with database_session_factory() as session:
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert availability is not None
        expected_version = availability.version
    await set_rider_availability_intent(
        database_session_factory,
        rider_id=fixture.rider_ids[0],
        intent=INTENT_OFFLINE,
        expected_version=expected_version,
    )

    replay = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind=TARGET_PICKUP,
        target_id=pickup_id,
        client_id=client_id,
    )

    async with database_session_factory() as session:
        predecessor = await session.get(RiderAssignment, predecessor.assignment_id)
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        capture_count = await session.scalar(select(func.count()).select_from(EvidenceCapture))
    assert replay.evidence_capture_id == first.evidence_capture_id
    assert predecessor is not None and predecessor.status == "SUPERSEDED"
    assert availability is not None and availability.availability_intent == INTENT_OFFLINE
    assert capture_count == 1


async def test_handover_capture_replays_after_master_and_handover_history_changes(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, point, rejected = await create_handover_fixture(
        database_session_factory,
        validated=False,
    )
    client_id = new_uuid7()
    first = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind=TARGET_HANDOVER,
        target_id=rejected.handover_event_id,
        client_id=client_id,
    )
    later = await record_handover(
        database_session_factory,
        fixture,
        point,
        fixture.pickup_ids[0],
        observed_location=GeoPoint(latitude=28.6139, longitude=77.2090),
    )
    async with database_session_factory() as session, session.begin():
        persisted_point = await session.get(ReceivingPoint, point.receiving_point_id)
        assert persisted_point is not None
        persisted_point.status = "INACTIVE"
        persisted_point.location = WKTElement("POINT(80 30)", srid=4326)
        persisted_point.allowed_radius_m = 1
        persisted_point.version += 1

    replay = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind=TARGET_HANDOVER,
        target_id=rejected.handover_event_id,
        client_id=client_id,
    )

    assert replay.evidence_capture_id == first.evidence_capture_id
    assert later.status == "VALIDATED"


async def test_concurrent_exact_capture_commands_converge_on_one_capture_and_link(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    pickup_id = fixture.pickup_ids[0][0]
    client_id = new_uuid7()

    results = await asyncio.gather(
        *(
            capture(
                database_session_factory,
                actor_id=fixture.rider_ids[0],
                target_kind=TARGET_PICKUP,
                target_id=pickup_id,
                client_id=client_id,
            )
            for _ in range(2)
        )
    )

    async with database_session_factory() as session:
        capture_count = await session.scalar(select(func.count()).select_from(EvidenceCapture))
        pickup_link_count = await session.scalar(
            select(func.count()).select_from(PickupEvidenceLink)
        )
        handover_link_count = await session.scalar(
            select(func.count()).select_from(HandoverEvidenceLink)
        )
        idempotency_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(
                IdempotencyRecord.scope == EVIDENCE_CAPTURE_IDEMPOTENCY_SCOPE,
                IdempotencyRecord.status == "COMPLETED",
            )
        )
    assert results[0].evidence_capture_id == results[1].evidence_capture_id
    assert (capture_count, pickup_link_count, handover_link_count, idempotency_count) == (
        1,
        1,
        0,
        1,
    )


async def test_late_failure_rolls_back_capture_link_and_idempotency(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)

    async def fail_completion(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected late evidence failure")

    monkeypatch.setattr(evidence_service, "complete_idempotency_record", fail_completion)
    with pytest.raises(RuntimeError, match="injected late evidence failure"):
        await capture(
            database_session_factory,
            actor_id=fixture.rider_ids[0],
            target_kind=TARGET_PICKUP,
            target_id=fixture.pickup_ids[0][0],
        )

    async with database_session_factory() as session:
        capture_count = await session.scalar(select(func.count()).select_from(EvidenceCapture))
        pickup_link_count = await session.scalar(
            select(func.count()).select_from(PickupEvidenceLink)
        )
        idempotency_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(IdempotencyRecord.scope == EVIDENCE_CAPTURE_IDEMPOTENCY_SCOPE)
        )
    assert (capture_count, pickup_link_count, idempotency_count) == (0, 0, 0)
