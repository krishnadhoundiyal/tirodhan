from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from test_fleet_dispatch import fixture
from test_planning_result import create_started_batch, normal_result

from tirodhan.db.values import new_uuid7
from tirodhan.modules.dispatch.events import DISPATCH_REQUESTED, FLEET_FIRST, append_dispatch_event
from tirodhan.modules.planning.models import CollectionGroup, PlanningBatch
from tirodhan.modules.planning.service import (
    commit_planning_result,
    record_planning_technical_failure,
)
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.reliability.primitives import append_outbox_event
from tirodhan.modules.reliability.publisher import publish_outbox_batch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("fallback", [False, True])
async def test_planning_normal_and_fallback_emit_per_group_once(database_session_factory, fallback):
    factory = database_session_factory
    batch, attempt, request_ids, message = await create_started_batch(factory, max_attempts=1)

    async def operation():
        if fallback:
            return await record_planning_technical_failure(
                factory,
                planning_batch_id=batch.planning_batch_id,
                planning_batch_attempt_id=attempt.planning_batch_attempt_id,
                failure_code="TEST_FAILURE",
                message_id=message.message_id,
            )
        return await commit_planning_result(
            factory, normal_result(batch, attempt, request_ids), message_id=message.message_id
        )

    await operation()
    async with factory() as session:
        groups = tuple(await session.scalars(select(CollectionGroup.collection_group_id)))
        events = tuple(
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == DISPATCH_REQUESTED)
            )
        )
        assert len(groups) == (3 if fallback else 2)
        assert {event.aggregate_id for event in events} == set(groups)
        assert all(
            event.event_key == f"collection-group-dispatch:{event.aggregate_id}:FLEET_FIRST"
            for event in events
        )
        assert all(event.payload["dispatch_stage"] == "FLEET_FIRST" for event in events)
        assert all(
            set(event.payload)
            == {
                "collection_group_id",
                "planning_batch_id",
                "cell_id",
                "slot_start",
                "slot_end",
                "dispatch_stage",
            }
            for event in events
        )
    await operation()
    async with factory() as session:
        replay = tuple(
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == DISPATCH_REQUESTED)
            )
        )
        assert {event.outbox_event_id for event in replay} == {
            event.outbox_event_id for event in events
        }


async def test_publisher_explicit_routes_send_before_mark_and_no_arbitrary_events(
    database_session_factory, database_engine
):
    factory = database_session_factory
    value = await fixture(factory)
    context_id = new_uuid7()
    async with factory() as session, session.begin():
        group = await session.get(CollectionGroup, value.group_ids[0])
        batch = await session.get(PlanningBatch, group.planning_batch_id)
        await append_dispatch_event(
            session, group_id=group.collection_group_id, batch=batch, stage=FLEET_FIRST
        )
        await append_outbox_event(
            session,
            event_key="serviceability-test",
            aggregate_type="serviceability_context",
            aggregate_id=context_id,
            event_type="ServiceabilityRequested",
            payload={"serviceability_context_id": str(context_id)},
        )
        await append_outbox_event(
            session,
            event_key="unregistered-test",
            aggregate_type="other",
            aggregate_id=new_uuid7(),
            event_type="UnknownEvent",
            payload={},
        )

    class Publisher:
        def __init__(self):
            self.sent = []

        async def send(self, entity, message):
            assert database_engine.pool.checkedout() == 0
            async with factory() as session:
                event = await session.get(OutboxEvent, __import__("uuid").UUID(message.message_id))
                assert event.status == "PENDING"
            self.sent.append((entity, message))

    publisher = Publisher()
    assert (
        await publish_outbox_batch(
            factory,
            publisher,
            serviceability_entity="serviceability",
            rider_notification_entity="rider-notifications",
            batch_size=10,
        )
        == 2
    )
    assert {entity for entity, _ in publisher.sent} == {"serviceability", "rider-notifications"}
    assert next(
        json.loads(message.body) for entity, message in publisher.sent if entity == "serviceability"
    ) == {"serviceability_context_id": str(context_id)}
    dispatch = next(
        message for entity, message in publisher.sent if entity == "rider-notifications"
    )
    assert set(json.loads(dispatch.body)) == {
        "collection_group_id",
        "planning_batch_id",
        "cell_id",
        "slot_start",
        "slot_end",
        "dispatch_stage",
    }
    async with factory() as session:
        assert (
            await session.scalar(
                select(OutboxEvent).where(OutboxEvent.event_type == "UnknownEvent")
            )
        ).status == "PENDING"
        assert {
            event.status
            for event in await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type.in_([DISPATCH_REQUESTED, "ServiceabilityRequested"])
                )
            )
        } == {"PUBLISHED"}
