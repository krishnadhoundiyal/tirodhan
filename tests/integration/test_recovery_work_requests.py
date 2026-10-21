"""Real PostgreSQL publication/processing boundaries; broker transport is controlled."""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_rider_failure_recovery import choose, failed
from test_routing_fleet import LineMatrix, engines

from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent
from tirodhan.modules.reliability.publisher import publish_outbox_batch
from tirodhan.modules.routing_operations.capacity_models import RiderReplacement
from tirodhan.modules.routing_operations.failure_execution import (
    activate_recovery,
    claim_activation,
    finish_attempt,
)
from tirodhan.modules.routing_operations.failure_models import (
    RecoveryAssignment,
    RecoveryWorkRequest,
)
from tirodhan.modules.routing_operations.failure_runtime import recommend_one, reconcile_recovery
from tirodhan.modules.routing_operations.recovery_work import (
    current_work,
    publication_claim,
    publication_finished,
)
from tirodhan.workers.planning_worker import handle_planning_delivery

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class Publisher:
    def __init__(self):
        self.sent = []

    async def send(self, entity, message):
        self.sent.append(message)


async def publish(factory, sender):
    return await publish_outbox_batch(
        factory,
        sender,
        serviceability_entity="serviceability",
        planning_entity="planning",
        batch_size=100,
    )


class Delivery:
    message_type = "RecoveryWorkRequested"

    def __init__(self, message, *, lose_completion=False):
        self.message_id, self.body = message.message_id, message.body
        self.completed = self.abandoned = self.dead_lettered = 0
        self.lose_completion = lose_completion

    async def complete(self):
        if self.lose_completion:
            self.lose_completion = False
            raise RuntimeError("SIMULATED_COMPLETION_LOSS")
        self.completed += 1

    async def abandon(self):
        self.abandoned += 1

    async def dead_letter(self):
        self.dead_lettered += 1


async def allocation(factory):
    data, incident, root = await failed(factory)
    row, leg = await choose(factory, data, incident)
    async with factory() as session:
        work = await current_work(session, leg.assignment_id, "ACTIVATE")
        assert work and work.generation == 1
    return data, row, leg, work, root


def clock(monkeypatch, now):
    from tirodhan.modules.reliability import publisher
    from tirodhan.modules.routing_operations import recovery_work

    times = [now]
    monkeypatch.setattr(publisher, "utc_now", lambda: times[0])
    monkeypatch.setattr(recovery_work, "utc_now", lambda: times[0])
    return times


async def test_delayed_consumers_and_concurrent_scans_do_not_manufacture_work(
    database_session_factory,
    monkeypatch,
):
    factory = database_session_factory
    data, _, leg, work, _ = await allocation(factory)
    times = clock(monkeypatch, data[6][0])
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(RecoveryWorkRequest))
        version = (await session.get(RecoveryAssignment, leg.assignment_id)).version
        request_attempts = list(
            (
                await session.execute(
                    select(RiderReplacement.replacement_id, RiderReplacement.attempt)
                )
            ).all()
        )
    sender = Publisher()
    await publish(factory, sender)
    assert sum(m.message_id == str(work.outbox_event_id) for m in sender.sent) == 1
    for _ in range(5):
        times[0] += timedelta(minutes=10)
        assert await asyncio.gather(
            reconcile_recovery(factory, times[0]), reconcile_recovery(factory, times[0])
        ) == [0, 0]
        await publish(factory, sender)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RecoveryWorkRequest)) == count
        assignment = await session.get(RecoveryAssignment, leg.assignment_id)
        assert assignment.version == version and assignment.attempt == 0
        assert (
            list(
                (
                    await session.execute(
                        select(RiderReplacement.replacement_id, RiderReplacement.attempt)
                    )
                ).all()
            )
            == request_attempts
        )
        assert (await session.get(RecoveryWorkRequest, work.outbox_event_id)).state == "PUBLISHED"
        assert (await session.get(OutboxEvent, work.outbox_event_id)).publish_attempt_count == 1
    assert sum(m.message_id == str(work.outbox_event_id) for m in sender.sent) == 1


async def test_overlapping_publishers_have_one_live_send_claim(
    database_session_factory, monkeypatch
):
    factory = database_session_factory
    data, _, _, work, _ = await allocation(factory)
    clock(monkeypatch, data[6][0])
    entered, release = asyncio.Event(), asyncio.Event()

    class Paused(Publisher):
        async def send(self, entity, message):
            await super().send(entity, message)
            if message.message_id == str(work.outbox_event_id):
                entered.set()
                await release.wait()

    sender = Paused()
    task = asyncio.create_task(publish(factory, sender))
    try:
        await asyncio.wait_for(entered.wait(), 15)
        await publish(factory, sender)
        assert sum(m.message_id == str(work.outbox_event_id) for m in sender.sent) == 1
    finally:
        release.set()
        await task
    async with factory() as session:
        assert (await session.get(OutboxEvent, work.outbox_event_id)).publish_attempt_count == 1


async def test_uncertain_send_retries_same_identity_without_processing_attempt(
    database_session_factory,
    monkeypatch,
):
    factory = database_session_factory
    data, _, leg, work, _ = await allocation(factory)
    times = clock(monkeypatch, data[6][0])

    class Ambiguous(Publisher):
        async def send(self, entity, message):
            await super().send(entity, message)
            if message.message_id == str(work.outbox_event_id):
                raise TimeoutError("SIMULATED_SEND_ACK_LOSS")

    sender = Ambiguous()
    await publish(factory, sender)
    async with factory() as session:
        assert (await session.get(RecoveryWorkRequest, work.outbox_event_id)).state == "RETRY_DUE"
        event = await session.get(OutboxEvent, work.outbox_event_id)
        assert event.status == "PENDING" and event.publish_attempt_count == 1
        assert (await session.get(RecoveryAssignment, leg.assignment_id)).attempt == 0
        due = event.available_at
    good = Publisher()
    await publish(factory, good)
    assert not any(m.message_id == str(work.outbox_event_id) for m in good.sent)
    times[0] = due
    await publish(factory, good)
    message = next(m for m in good.sent if m.message_id == str(work.outbox_event_id))
    assert json.loads(message.body)["generation"] == 1
    async with factory() as session:
        assert (await session.get(OutboxEvent, work.outbox_event_id)).publish_attempt_count == 2
        assert (await session.get(RecoveryAssignment, leg.assignment_id)).attempt == 0


async def test_expired_publication_lease_fences_old_publisher(
    database_session_factory, monkeypatch
):
    factory = database_session_factory
    data, _, leg, work, _ = await allocation(factory)
    now = data[6][0]
    clock(monkeypatch, now)
    first = await publication_claim(factory, work.outbox_event_id, now)
    assert first
    assert (
        await publication_claim(factory, work.outbox_event_id, now + timedelta(seconds=59)) is None
    )
    successor = await publication_claim(factory, work.outbox_event_id, now + timedelta(seconds=61))
    assert successor and successor != first
    await publication_finished(factory, work.outbox_event_id, first, success=True)
    async with factory() as session:
        assert (
            await session.get(RecoveryWorkRequest, work.outbox_event_id)
        ).publication_token == successor
        assert (await session.get(OutboxEvent, work.outbox_event_id)).status == "PENDING"
    await publication_finished(factory, work.outbox_event_id, successor, success=True)
    assert await publication_claim(factory, work.outbox_event_id, now + timedelta(days=1)) is None


@pytest.mark.parametrize("crash_boundary", ["domain_commit", "message_completion"])
async def test_broker_redelivery_and_operational_retry_are_distinct(
    database_session_factory,
    monkeypatch,
    crash_boundary,
):
    from tirodhan.modules.planning.routing.execution import RoutingAdapterError
    from tirodhan.workers import routing_recovery

    factory = database_session_factory
    data, _, leg, work, _ = await allocation(factory)
    times = clock(monkeypatch, data[6][0])
    sender = Publisher()
    await publish(factory, sender)
    message = next(m for m in sender.sent if m.message_id == str(work.outbox_event_id))
    delivery = Delivery(message)
    calls = []

    class Broken:
        async def acquire(self, nodes):
            raise RoutingAdapterError("SIMULATED_PROVIDER_OUTAGE")

    async def execute(actual_factory, identity, kind, settings, *, generation):
        calls.append(generation)
        if len(calls) == 1:
            raise RuntimeError("SIMULATED_INFRASTRUCTURE_FAILURE")
        result = await activate_recovery(
            actual_factory,
            identity,
            generation=generation,
            now=times[0],
            providers={"test-car": Broken() if len(calls) == 2 else LineMatrix()},
            optimizers=engines(),
        )
        if len(calls) == 2 and crash_boundary == "domain_commit":
            raise RuntimeError("SIMULATED_CRASH_BEFORE_INBOX_COMPLETION")
        return result

    monkeypatch.setattr(routing_recovery, "execute", execute)
    await handle_planning_delivery(delivery, factory)
    assert delivery.abandoned == 1 and delivery.completed == 0
    assert await reconcile_recovery(factory, times[0]) == 0
    async with factory() as session:
        assert (await session.get(RecoveryAssignment, leg.assignment_id)).attempt == 0
        assert (await current_work(session, leg.assignment_id, "ACTIVATE")).generation == 1

    # Broker redelivers the original message. Operational failure commits a new due request.
    delivery.lose_completion = crash_boundary == "message_completion"
    await handle_planning_delivery(delivery, factory)
    assert delivery.abandoned == 2  # Database commit succeeded; settlement response was lost.
    async with factory() as session:
        current = await current_work(session, leg.assignment_id, "ACTIVATE")
        assignment = await session.get(RecoveryAssignment, leg.assignment_id)
        assert current.generation == 2 and current.state == "READY"
        assert assignment.status == "PENDING" and assignment.attempt == 1
        expected_inbox = "PROCESSING" if crash_boundary == "domain_commit" else "PROCESSED"
        assert (
            await session.get(InboxMessage, ("routing-recovery", message.message_id))
        ).status == expected_inbox
        event = await session.get(OutboxEvent, current.outbox_event_id)
        assert event.available_at == assignment.due_at > times[0]
        retry_id, due = current.outbox_event_id, assignment.due_at
    await handle_planning_delivery(delivery, factory)
    assert delivery.completed == 1 and calls == [1, 1]
    assert await reconcile_recovery(factory, times[0] + timedelta(seconds=1)) == 0
    sender.sent.clear()
    await publish(factory, sender)
    assert not any(m.message_id == str(retry_id) for m in sender.sent)
    times[0] = due
    await publish(factory, sender)
    retry = Delivery(next(m for m in sender.sent if m.message_id == str(retry_id)))
    await handle_planning_delivery(retry, factory)
    assert retry.completed == 1 and calls == [1, 1, 2]
    async with factory() as session:
        assignment = await session.get(RecoveryAssignment, leg.assignment_id)
        assert assignment.status == "ACTIVATED" and assignment.attempt == 2
        assert await current_work(session, leg.assignment_id, "ACTIVATE") is None


async def test_expired_execution_authorizes_one_generation_and_fences_old_delivery(
    database_session_factory,
    monkeypatch,
):
    from tirodhan.workers import routing_recovery

    factory = database_session_factory
    data, _, leg, work, _ = await allocation(factory)
    now = data[6][0]
    times = clock(monkeypatch, now)
    sender = Publisher()
    await publish(factory, sender)
    message = next(m for m in sender.sent if m.message_id == str(work.outbox_event_id))
    token = await claim_activation(factory, leg.assignment_id, now, generation=1)
    assert token
    times[0] += timedelta(seconds=121)
    assert (
        sum(
            await asyncio.gather(
                reconcile_recovery(factory, times[0]), reconcile_recovery(factory, times[0])
            )
        )
        == 1
    )
    assert await reconcile_recovery(factory, times[0]) == 0
    async with factory() as session:
        pending = await current_work(session, leg.assignment_id, "ACTIVATE")
        assert pending.generation == 2 and pending.started_at is None
        assert (await session.get(RecoveryAssignment, leg.assignment_id)).attempt == 1

    async def forbidden(*args, **kwargs):
        raise AssertionError("stale generation must be fenced before graph startup")

    monkeypatch.setattr(routing_recovery, "execute", forbidden)
    delivery = Delivery(message)
    await handle_planning_delivery(delivery, factory)
    assert delivery.completed == 1 and delivery.abandoned == 0
    await finish_attempt(factory, leg.assignment_id, token, times[0], "OLD_OWNER")
    async with factory() as session:
        assert (await current_work(session, leg.assignment_id, "ACTIVATE")).generation == 2


async def test_retry_outcome_and_next_outbox_roll_back_together(
    database_session_factory, monkeypatch
):
    from tirodhan.modules.routing_operations import recovery_work

    factory = database_session_factory
    data, _, leg, work, _ = await allocation(factory)
    now = data[6][0]
    token = await claim_activation(factory, leg.assignment_id, now, generation=1)
    original = recovery_work.enqueue_work

    async def fail_after_append(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("SIMULATED_COMMIT_BOUNDARY_FAILURE")

    monkeypatch.setattr(recovery_work, "enqueue_work", fail_after_append)
    with pytest.raises(RuntimeError, match="COMMIT_BOUNDARY"):
        await finish_attempt(factory, leg.assignment_id, token, now, "PROVIDER_TIMEOUT")
    async with factory() as session:
        row = await session.get(RecoveryAssignment, leg.assignment_id)
        assert row.status == "RUNNING" and row.lease_token == token
        assert (await current_work(session, leg.assignment_id, "ACTIVATE")).generation == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RecoveryWorkRequest)
                .where(RecoveryWorkRequest.assignment_id == leg.assignment_id)
            )
            == 1
        )


async def test_recommendation_outcome_commits_its_retry_and_counts_only_processing(
    database_session_factory,
):
    from tirodhan.modules.planning.routing.execution import RoutingAdapterError

    factory = database_session_factory
    data, incident, _ = await failed(factory)
    async with factory() as session:
        row = await session.scalar(
            select(RiderReplacement).where(RiderReplacement.failure_incident_id == incident)
        )
        original = await current_work(session, row.replacement_id, "RECOMMEND")

    class Broken:
        async def acquire(self, nodes):
            raise RoutingAdapterError("SIMULATED_PROVIDER_OUTAGE")

    assert not await recommend_one(
        factory,
        row.replacement_id,
        generation=1,
        providers={"test-car": Broken()},
        optimizers=engines(),
        now=data[6][0],
    )
    async with factory() as session:
        row = await session.get(RiderReplacement, row.replacement_id)
        work = await current_work(session, row.replacement_id, "RECOMMEND")
        assert row.attempt == 1 and row.recommendation_token is None
        assert work.generation == 2 and work.started_at is None
        assert (await session.get(RecoveryWorkRequest, original.outbox_event_id)).state == "CLOSED"
        assert (
            await session.get(OutboxEvent, work.outbox_event_id)
        ).available_at == row.next_attempt_at
        due = row.next_attempt_at
    assert await reconcile_recovery(factory, due) == 0
    assert await recommend_one(
        factory,
        row.replacement_id,
        generation=2,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=due,
    )
    async with factory() as session:
        assert (await session.get(RiderReplacement, row.replacement_id)).attempt == 2
        assert await current_work(session, row.replacement_id, "RECOMMEND") is None


async def test_live_execution_causes_broker_abandon_without_new_publication(
    database_session_factory,
    monkeypatch,
):
    from tirodhan.workers import routing_recovery

    factory = database_session_factory
    data, _, leg, work, _ = await allocation(factory)
    clock(monkeypatch, data[6][0])
    sender = Publisher()
    await publish(factory, sender)
    delivery = Delivery(next(m for m in sender.sent if m.message_id == str(work.outbox_event_id)))
    assert await claim_activation(factory, leg.assignment_id, data[6][0], generation=1)

    async def execute(actual_factory, identity, kind, settings, *, generation):
        await activate_recovery(
            actual_factory,
            identity,
            generation=generation,
            now=data[6][0],
            providers={},
            optimizers={},
        )

    monkeypatch.setattr(routing_recovery, "execute", execute)
    await handle_planning_delivery(delivery, factory)
    assert delivery.abandoned == 1 and delivery.completed == 0
    assert await reconcile_recovery(factory, data[6][0]) == 0
    async with factory() as session:
        assert (await current_work(session, leg.assignment_id, "ACTIVATE")).generation == 1
        assert (await session.get(RecoveryAssignment, leg.assignment_id)).attempt == 1
        assert (await session.get(OutboxEvent, work.outbox_event_id)).publish_attempt_count == 1


async def test_hung_send_is_bounded_and_retains_same_retry_identity(
    database_session_factory,
    monkeypatch,
):
    from tirodhan.modules.reliability import publisher

    factory = database_session_factory
    data, _, leg, work, _ = await allocation(factory)
    clock(monkeypatch, data[6][0])
    monkeypatch.setattr(publisher, "RECOVERY_SEND_TIMEOUT_SECONDS", 0.01)

    class Hung(Publisher):
        async def send(self, entity, message):
            await asyncio.Event().wait()

    await publish(factory, Hung())
    async with factory() as session:
        current = await current_work(session, leg.assignment_id, "ACTIVATE")
        assert current.outbox_event_id == work.outbox_event_id and current.state == "RETRY_DUE"
        assert current.generation == 1
        assert (await session.get(RecoveryAssignment, leg.assignment_id)).attempt == 0
