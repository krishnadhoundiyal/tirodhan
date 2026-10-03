from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import select
from test_fleet_dispatch import fixture

from tirodhan.db.values import new_uuid7
from tirodhan.modules.dispatch.events import FLEET_FIRST, append_dispatch_event
from tirodhan.modules.planning.models import CollectionGroup, PlanningBatch
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.reliability.primitives import append_outbox_event
from tirodhan.modules.reliability.publisher import publish_outbox_batch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("refund_send_fails", [False, True])
async def test_three_explicit_routes_send_before_published_and_unknown_stays_pending(
    database_session_factory, database_engine, refund_send_fails
):
    factory = database_session_factory
    value = await fixture(factory)
    refund_id, context_id = new_uuid7(), new_uuid7()
    async with factory() as session, session.begin():
        group = await session.get(CollectionGroup, value.group_ids[0])
        batch = await session.get(PlanningBatch, group.planning_batch_id)
        await append_dispatch_event(
            session, group_id=group.collection_group_id, batch=batch, stage=FLEET_FIRST
        )
        for kind, aggregate, identity, payload in [
            (
                "ServiceabilityRequested",
                "serviceability_context",
                context_id,
                {"serviceability_context_id": str(context_id)},
            ),
            ("RefundRequested", "refund", refund_id, {"refund_id": str(refund_id)}),
            ("UnknownEvent", "unknown", new_uuid7(), {}),
        ]:
            await append_outbox_event(
                session,
                event_key=kind,
                event_type=kind,
                aggregate_type=aggregate,
                aggregate_id=identity,
                payload=payload,
            )

    class Publisher:
        def __init__(self):
            self.sent = []

        async def send(self, entity, message):
            assert database_engine.pool.checkedout() == 0
            async with factory() as session:
                event = await session.get(OutboxEvent, UUID(message.message_id))
                assert event.status == "PENDING"
            self.sent.append((entity, message))
            if entity == "refunds" and refund_send_fails:
                raise RuntimeError("untrusted provider exception")

    publisher = Publisher()
    published = await publish_outbox_batch(
        factory,
        publisher,
        serviceability_entity="serviceability",
        rider_notification_entity="notifications",
        refund_entity="refunds",
        batch_size=10,
    )
    assert published == (2 if refund_send_fails else 3)
    assert {entity for entity, _ in publisher.sent} == {
        "serviceability",
        "notifications",
        "refunds",
    }
    assert json.loads(
        next(message.body for entity, message in publisher.sent if entity == "refunds")
    ) == {"refund_id": str(refund_id)}
    async with factory() as session:
        events = {row.event_type: row for row in await session.scalars(select(OutboxEvent))}
        assert (
            events["ServiceabilityRequested"].status
            == events["CollectionGroupDispatchRequested"].status
            == "PUBLISHED"
        )
        assert events["UnknownEvent"].status == "PENDING"
        assert events["RefundRequested"].status == ("PENDING" if refund_send_fails else "PUBLISHED")


async def test_refund_route_requires_queue_and_valid_identifier_only_metadata(
    database_session_factory,
):
    factory = database_session_factory
    refund_id = new_uuid7()
    async with factory() as session, session.begin():
        event = await append_outbox_event(
            session,
            event_key="invalid-refund",
            aggregate_type="refund",
            aggregate_id=refund_id,
            event_type="RefundRequested",
            payload={"refund_id": str(new_uuid7())},
        )

    class NeverSend:
        async def send(self, entity, message):
            pytest.fail("unconfigured or invalid route must not send")

    for queue in [None, "refunds"]:
        assert (
            await publish_outbox_batch(
                factory,
                NeverSend(),
                serviceability_entity="serviceability",
                refund_entity=queue,
                batch_size=10,
            )
            == 0
        )
    async with factory() as session:
        assert (await session.get(OutboxEvent, event.outbox_event_id)).status == "PENDING"
