from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_evidence_capture import capture
from test_handover import assign_and_collect, create_receiving_point, record
from test_media_asset import register
from test_rider_dispatch import create_fixture

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests import service as completion_service
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.service import (
    CollectionRequestHandoverEvidenceInsufficientError,
    CollectionRequestNotCompletableError,
    CollectionRequestNotFoundError,
    CollectionRequestPickupEvidenceInsufficientError,
    CollectionRequestPickupMissingError,
    CollectionRequestPickupNotCollectedError,
    CollectionRequestValidatedHandoverMissingError,
    complete_collection_request,
)
from tirodhan.modules.customers.service import GeoPoint
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem
from tirodhan.modules.dispatch.service import assign_group_manually
from tirodhan.modules.evidence.models import EvidenceCapture, MediaAsset
from tirodhan.modules.handovers.models import HandoverEvent, HandoverEventItem
from tirodhan.modules.operations.service import open_pickup_incident
from tirodhan.modules.pickups.models import PickupIncident
from tirodhan.modules.pickups.service import (
    ATTEMPT_COLLECTED,
    record_pickup_attempt,
    start_assignment,
)
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.reliability.models import IdempotencyRecord, OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

COMPLETED_AT = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
REJECTED_LOCATION = GeoPoint(latitude=28.7, longitude=77.3)


@dataclass(frozen=True, slots=True)
class ReadyRequest:
    request_id: UUID
    pickup_id: UUID
    event_id: UUID
    rider_id: UUID
    assignment_id: UUID
    pickup_evidence: EvidenceCapture | None
    handover_evidence: EvidenceCapture | None


async def request_id_for_pickup(factory: async_sessionmaker[AsyncSession], pickup_id: UUID) -> UUID:
    async with factory() as session:
        request_id = await session.scalar(
            select(PickupExecution.request_id).where(
                PickupExecution.pickup_execution_id == pickup_id
            )
        )
    assert request_id is not None
    return request_id


async def request_state(
    factory: async_sessionmaker[AsyncSession], request_id: UUID
) -> tuple[str, datetime | None]:
    async with factory() as session:
        request = await session.get(CollectionRequest, request_id)
    assert request is not None
    return request.status, request.completed_at


async def ready_request(
    factory: async_sessionmaker[AsyncSession],
    *,
    pickup_evidence: bool = True,
    handover_evidence: bool = True,
) -> ReadyRequest:
    fixture, assignment = await assign_and_collect(factory, pickup_count=1)
    pickup_id = fixture.pickup_ids[0][0]
    point = await create_receiving_point(factory)
    event = await record(factory, fixture, point, (pickup_id,))
    pickup_capture = (
        await capture(
            factory,
            actor_id=fixture.rider_ids[0],
            target_kind="PICKUP",
            target_id=pickup_id,
        )
        if pickup_evidence
        else None
    )
    handover_capture = (
        await capture(
            factory,
            actor_id=fixture.rider_ids[0],
            target_kind="HANDOVER",
            target_id=event.handover_event_id,
        )
        if handover_evidence
        else None
    )
    return ReadyRequest(
        request_id=await request_id_for_pickup(factory, pickup_id),
        pickup_id=pickup_id,
        event_id=event.handover_event_id,
        rider_id=fixture.rider_ids[0],
        assignment_id=assignment.assignment_id,
        pickup_evidence=pickup_capture,
        handover_evidence=handover_capture,
    )


async def test_minimum_evidence_completes_without_media_assets(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ready = await ready_request(database_session_factory)

    completed = await complete_collection_request(
        database_session_factory, ready.request_id, now=COMPLETED_AT
    )

    async with database_session_factory() as session:
        media_count = await session.scalar(select(func.count()).select_from(MediaAsset))
    assert (completed.status, completed.completed_at) == ("COMPLETED", COMPLETED_AT)
    assert media_count == 0


async def test_missing_pickup_fails_without_request_mutation(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=1, rider_count=2, pickups_per_group=1
    )
    pickup_id = fixture.pickup_ids[0][0]
    request_id = await request_id_for_pickup(database_session_factory, pickup_id)
    async with database_session_factory() as session, session.begin():
        pickup = await session.get(PickupExecution, pickup_id)
        assert pickup is not None
        await session.delete(pickup)

    with pytest.raises(CollectionRequestPickupMissingError):
        await complete_collection_request(database_session_factory, request_id)
    assert await request_state(database_session_factory, request_id) == ("PLANNED", None)


async def test_uncollected_pickup_fails_without_request_mutation(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=1, rider_count=2, pickups_per_group=1
    )
    request_id = await request_id_for_pickup(database_session_factory, fixture.pickup_ids[0][0])

    with pytest.raises(CollectionRequestPickupNotCollectedError):
        await complete_collection_request(database_session_factory, request_id)
    assert await request_state(database_session_factory, request_id) == ("PLANNED", None)


async def test_collected_pickup_without_validated_handover_fails(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    pickup_id = fixture.pickup_ids[0][0]
    request_id = await request_id_for_pickup(database_session_factory, pickup_id)
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="PICKUP",
        target_id=pickup_id,
    )

    with pytest.raises(CollectionRequestValidatedHandoverMissingError):
        await complete_collection_request(database_session_factory, request_id)
    assert await request_state(database_session_factory, request_id) == ("PLANNED", None)


async def test_rejected_handover_and_its_evidence_never_satisfy_completion(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    pickup_id = fixture.pickup_ids[0][0]
    point = await create_receiving_point(database_session_factory, radius_m=50)
    rejected = await record(
        database_session_factory,
        fixture,
        point,
        (pickup_id,),
        observed_location=REJECTED_LOCATION,
    )
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="PICKUP",
        target_id=pickup_id,
    )
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="HANDOVER",
        target_id=rejected.handover_event_id,
    )

    request_id = await request_id_for_pickup(database_session_factory, pickup_id)
    with pytest.raises(CollectionRequestValidatedHandoverMissingError):
        await complete_collection_request(database_session_factory, request_id)


async def test_historical_rejection_then_validated_handover_completes(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    pickup_id = fixture.pickup_ids[0][0]
    point = await create_receiving_point(database_session_factory, radius_m=50)
    await record(
        database_session_factory,
        fixture,
        point,
        (pickup_id,),
        observed_location=REJECTED_LOCATION,
    )
    validated = await record(database_session_factory, fixture, point, (pickup_id,))
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="PICKUP",
        target_id=pickup_id,
    )
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="HANDOVER",
        target_id=validated.handover_event_id,
    )

    completed = await complete_collection_request(
        database_session_factory,
        await request_id_for_pickup(database_session_factory, pickup_id),
    )
    assert completed.status == "COMPLETED"


async def test_missing_pickup_evidence_fails(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ready = await ready_request(database_session_factory, pickup_evidence=False)
    with pytest.raises(CollectionRequestPickupEvidenceInsufficientError):
        await complete_collection_request(database_session_factory, ready.request_id)


async def test_missing_validated_handover_evidence_fails(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ready = await ready_request(database_session_factory, handover_evidence=False)
    with pytest.raises(CollectionRequestHandoverEvidenceInsufficientError):
        await complete_collection_request(database_session_factory, ready.request_id)


async def test_evidence_on_rejected_handover_does_not_cover_validated_handover(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=1)
    pickup_id = fixture.pickup_ids[0][0]
    point = await create_receiving_point(database_session_factory, radius_m=50)
    rejected = await record(
        database_session_factory,
        fixture,
        point,
        (pickup_id,),
        observed_location=REJECTED_LOCATION,
    )
    await record(database_session_factory, fixture, point, (pickup_id,))
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="PICKUP",
        target_id=pickup_id,
    )
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="HANDOVER",
        target_id=rejected.handover_event_id,
    )

    with pytest.raises(CollectionRequestHandoverEvidenceInsufficientError):
        await complete_collection_request(
            database_session_factory,
            await request_id_for_pickup(database_session_factory, pickup_id),
        )


async def test_pending_media_assets_do_not_block_completion(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ready = await ready_request(database_session_factory)
    assert ready.pickup_evidence is not None and ready.handover_evidence is not None
    assets = (
        await register(database_session_factory, evidence=ready.pickup_evidence),
        await register(database_session_factory, evidence=ready.handover_evidence),
    )

    completed = await complete_collection_request(database_session_factory, ready.request_id)

    assert completed.status == "COMPLETED"
    assert {asset.upload_status for asset in assets} == {"PENDING_UPLOAD"}


async def test_shared_handover_evidence_and_sibling_requests_are_independent(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment = await assign_and_collect(database_session_factory, pickup_count=2)
    pickup_a, pickup_b = fixture.pickup_ids[0]
    point = await create_receiving_point(database_session_factory)
    event = await record(database_session_factory, fixture, point, (pickup_a, pickup_b))
    for pickup_id in (pickup_a, pickup_b):
        await capture(
            database_session_factory,
            actor_id=fixture.rider_ids[0],
            target_kind="PICKUP",
            target_id=pickup_id,
        )
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="HANDOVER",
        target_id=event.handover_event_id,
    )
    request_a = await request_id_for_pickup(database_session_factory, pickup_a)
    request_b = await request_id_for_pickup(database_session_factory, pickup_b)

    completed = await complete_collection_request(database_session_factory, request_a)

    assert completed.status == "COMPLETED"
    assert await request_state(database_session_factory, request_b) == ("PLANNED", None)


async def test_active_assignment_does_not_block_request_completion(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment = await assign_and_collect(
        database_session_factory, pickup_count=2, collect_indexes=(0,)
    )
    pickup_id = fixture.pickup_ids[0][0]
    point = await create_receiving_point(database_session_factory)
    event = await record(database_session_factory, fixture, point, (pickup_id,))
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="PICKUP",
        target_id=pickup_id,
    )
    await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="HANDOVER",
        target_id=event.handover_event_id,
    )

    completed = await complete_collection_request(
        database_session_factory,
        await request_id_for_pickup(database_session_factory, pickup_id),
    )
    async with database_session_factory() as session:
        established = await session.get(RiderAssignment, assignment.assignment_id)
    assert completed.status == "COMPLETED"
    assert established is not None and established.status == "ACTIVE"


async def test_open_incident_does_not_block_and_completion_mutates_no_related_rows(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=1, rider_count=2, pickups_per_group=2
    )
    assignment = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    assignment = await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    pickup_id = fixture.pickup_ids[0][0]
    incident = await open_pickup_incident(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_incident_id=new_uuid7(),
        reason_code="OTHER",
    )
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome=ATTEMPT_COLLECTED,
    )
    point = await create_receiving_point(database_session_factory)
    event = await record(database_session_factory, fixture, point, (pickup_id,))
    pickup_evidence = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="PICKUP",
        target_id=pickup_id,
    )
    handover_evidence = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="HANDOVER",
        target_id=event.handover_event_id,
    )
    assets = (
        await register(database_session_factory, evidence=pickup_evidence),
        await register(database_session_factory, evidence=handover_evidence),
    )
    tracked: tuple[tuple[type[Any], Any], ...] = (
        (PickupExecution, pickup_id),
        (HandoverEvent, event.handover_event_id),
        (HandoverEventItem, (event.handover_event_id, pickup_id)),
        (EvidenceCapture, pickup_evidence.evidence_capture_id),
        (EvidenceCapture, handover_evidence.evidence_capture_id),
        (MediaAsset, assets[0].media_asset_id),
        (MediaAsset, assets[1].media_asset_id),
        (PickupIncident, incident.incident_id),
        (RiderAssignment, assignment.assignment_id),
        (RiderAssignmentItem, (assignment.assignment_id, pickup_id)),
    )
    before = await persisted_states(database_session_factory, tracked)
    async with database_session_factory() as session:
        outbox_before = await session.scalar(select(func.count()).select_from(OutboxEvent))
        idempotency_before = await session.scalar(
            select(func.count()).select_from(IdempotencyRecord)
        )

    completed = await complete_collection_request(
        database_session_factory,
        await request_id_for_pickup(database_session_factory, pickup_id),
    )

    after = await persisted_states(database_session_factory, tracked)
    async with database_session_factory() as session:
        outbox_after = await session.scalar(select(func.count()).select_from(OutboxEvent))
        idempotency_after = await session.scalar(
            select(func.count()).select_from(IdempotencyRecord)
        )
    assert completed.status == "COMPLETED"
    assert before == after
    assert outbox_after == outbox_before
    assert idempotency_after == idempotency_before
    assert after[(PickupIncident, incident.incident_id)][5] == "OPEN"


async def persisted_states(
    factory: async_sessionmaker[AsyncSession],
    identities: tuple[tuple[type[Any], Any], ...],
) -> dict[tuple[type[Any], Any], tuple[Any, ...]]:
    states: dict[tuple[type[Any], Any], tuple[Any, ...]] = {}
    async with factory() as session:
        for model, identity in identities:
            row = await session.get(model, identity)
            assert row is not None
            values: list[Any] = []
            for attribute in inspect(model).column_attrs:
                value = getattr(row, attribute.key)
                values.append(bytes(value.data) if hasattr(value, "data") else value)
            states[(model, identity)] = tuple(values)
    return states


async def test_completed_replay_preserves_timestamp_without_rechecking_prerequisites(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = await ready_request(database_session_factory)
    first = await complete_collection_request(
        database_session_factory, ready.request_id, now=COMPLETED_AT
    )

    async def fail_if_rechecked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("completed replay evaluated prerequisites")

    monkeypatch.setattr(completion_service, "_validate_completion_prerequisites", fail_if_rechecked)
    replay = await complete_collection_request(
        database_session_factory,
        ready.request_id,
        now=COMPLETED_AT + timedelta(hours=1),
    )
    assert first.completed_at == replay.completed_at == COMPLETED_AT
    assert replay.status == "COMPLETED"


@pytest.mark.parametrize(
    "status", ["PENDING_PAYMENT", "EXPIRED", "ACCEPTED", "PRE_PLANNING", "CANCELLED"]
)
async def test_nonplanned_request_states_are_not_completable(
    database_session_factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=1, rider_count=2, pickups_per_group=1
    )
    request_id = await request_id_for_pickup(database_session_factory, fixture.pickup_ids[0][0])
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == request_id)
            .values(status=status)
        )

    with pytest.raises(CollectionRequestNotCompletableError):
        await complete_collection_request(database_session_factory, request_id)
    assert await request_state(database_session_factory, request_id) == (status, None)


async def test_missing_request_raises_domain_error(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(CollectionRequestNotFoundError):
        await complete_collection_request(database_session_factory, new_uuid7())


async def test_concurrent_completion_converges_on_one_authoritative_timestamp(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ready = await ready_request(database_session_factory)
    other_time = COMPLETED_AT + timedelta(seconds=1)

    first, second = await asyncio.gather(
        complete_collection_request(database_session_factory, ready.request_id, now=COMPLETED_AT),
        complete_collection_request(database_session_factory, ready.request_id, now=other_time),
    )

    assert first.status == second.status == "COMPLETED"
    assert first.completed_at == second.completed_at
    assert first.completed_at in {COMPLETED_AT, other_time}
