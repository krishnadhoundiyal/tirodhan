"""Actual PostgreSQL incident, local repair and shared-authority recovery invariants."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_regional_parallel import prepared, run
from test_routing_fleet import LineMatrix, engines

from tirodhan.db.values import new_uuid7
from tirodhan.modules.dispatch.models import RiderAssignmentItem
from tirodhan.modules.routing_operations.capacity_models import RiderReplacement
from tirodhan.modules.routing_operations.dispatch_models import RouteActivation, VehicleRoute
from tirodhan.modules.routing_operations.failure_commands import (
    ApproveRecovery,
    FailRider,
    RecoverySelection,
    execute_failure,
)
from tirodhan.modules.routing_operations.failure_execution import (
    activate_recovery,
    claim_activation,
)
from tirodhan.modules.routing_operations.failure_models import (
    RecoveryAssignment,
    RecoveryAssignmentItem,
    RecoveryWitness,
    RiderFailureIncident,
)
from tirodhan.modules.routing_operations.failure_runtime import recommend_one
from tirodhan.modules.routing_operations.models import OperatingShift
from tirodhan.modules.routing_operations.replacement_models import ReplacementCandidate

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def failed(factory):
    data, _ = await prepared(factory)
    assert (await run(factory, data)).status == "ACTIVATED"
    async with factory() as session:
        root = await session.scalar(select(VehicleRoute).where(VehicleRoute.status == "ACTIVE"))
        shift = await session.get(OperatingShift, root.shift_id)
        roots = list(
            await session.scalars(
                select(VehicleRoute)
                .join(OperatingShift)
                .where(OperatingShift.rider_id == shift.rider_id, VehicleRoute.status != "CLOSED")
            )
        )
    command = FailRider(
        client_command_id=new_uuid7(),
        rider_id=shift.rider_id,
        reason="RIDER_UNAVAILABLE",
        expected_route_versions={r.route_id: r.version for r in roots},
    )
    incident = await execute_failure(
        factory, actor=data[0].manager_id, command=command, now=data[6][0]
    )
    assert (
        await execute_failure(factory, actor=data[0].manager_id, command=command, now=data[6][0])
        == incident
    )
    return data, incident, root


async def choose(factory, data, incident):
    async with factory() as session:
        row = await session.scalar(
            select(RiderReplacement).where(
                RiderReplacement.failure_incident_id == incident,
                RiderReplacement.status == "REQUESTED",
            )
        )
    assert await recommend_one(
        factory,
        row.replacement_id,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    async with factory() as session:
        row = await session.get(RiderReplacement, row.replacement_id)
        option = await session.scalar(
            select(ReplacementCandidate)
            .where(ReplacementCandidate.evaluation_id == row.evaluation_id)
            .order_by(ReplacementCandidate.rank)
        )
        parent = await session.get(RiderFailureIncident, incident)
    command = ApproveRecovery(
        client_command_id=new_uuid7(),
        incident_id=incident,
        expected_version=parent.version,
        selections=[
            RecoverySelection(
                candidate_id=option.candidate_id, replacement_ids=[row.replacement_id]
            )
        ],
        reason="RECOVERY",
    )
    await execute_failure(factory, actor=data[0].manager_id, command=command, now=data[6][0])
    assert (
        await execute_failure(factory, actor=data[0].manager_id, command=command, now=data[6][0])
        == incident
    )
    async with factory() as session:
        leg = await session.scalar(
            select(RecoveryAssignment)
            .join(RecoveryAssignmentItem)
            .where(
                RecoveryAssignment.incident_id == incident,
                RecoveryAssignmentItem.replacement_id == row.replacement_id,
                RecoveryAssignmentItem.released_at.is_(None),
            )
        )
    return row, leg


async def test_incident_covers_all_work_selection_does_not_release_and_activation_replays(
    database_session_factory,
):
    factory = database_session_factory
    data, incident, root = await failed(factory)
    async with factory() as session:
        old = list(
            await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == root.assignment_id,
                    RiderAssignmentItem.released_at.is_(None),
                )
            )
        )
        rows = list(
            await session.scalars(
                select(RiderReplacement).where(RiderReplacement.failure_incident_id == incident)
            )
        )
        assert len(rows) == len(old)
        assert (await session.get(VehicleRoute, root.route_id)).status == "RECOVERY_REQUIRED"
    row, leg = await choose(factory, data, incident)
    async with factory() as session:
        for item in old:
            assert (
                await session.get(
                    RiderAssignmentItem, (item.assignment_id, item.pickup_execution_id)
                )
            ).released_at is None
    result = await activate_recovery(
        factory,
        leg.assignment_id,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    async with factory() as session:
        updated = await session.get(RecoveryAssignment, leg.assignment_id)
        assert result is not None, updated.failure_code
        activation = await session.get(RouteActivation, result)
        assert activation.run_id is None and activation.recovery_witness_id
        assert (await session.get(RiderReplacement, row.replacement_id)).status == "COMPLETED"
        count = await session.scalar(select(func.count()).select_from(RecoveryWitness))
    assert (
        await activate_recovery(
            factory, leg.assignment_id, providers={}, optimizers={}, now=data[6][0]
        )
        == result
    )
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RecoveryWitness)) == count


async def test_activation_claim_and_expired_owner_are_fenced(database_session_factory):
    factory = database_session_factory
    data, incident, _ = await failed(factory)
    _, leg = await choose(factory, data, incident)
    at = data[6][0]
    a, b = await asyncio.gather(
        claim_activation(factory, leg.assignment_id, at),
        claim_activation(factory, leg.assignment_id, at),
    )
    assert (a is None) != (b is None)
    successor = await claim_activation(factory, leg.assignment_id, at + timedelta(seconds=121))
    assert successor and successor not in {a, b}
    from tirodhan.modules.routing_operations.failure_execution import finish_attempt

    await finish_attempt(
        factory, leg.assignment_id, a or b, at + timedelta(seconds=122), "OLD_OWNER"
    )
    async with factory() as session:
        assert (await session.get(RecoveryAssignment, leg.assignment_id)).lease_token == successor


async def test_provider_failure_preserves_ownership_then_retry_activates(database_session_factory):
    from tirodhan.modules.planning.routing.execution import RoutingAdapterError

    factory = database_session_factory
    data, incident, root = await failed(factory)
    _, leg = await choose(factory, data, incident)

    class Broken:
        async def acquire(self, nodes):
            raise RoutingAdapterError("PROVIDER_TIMEOUT")

    assert (
        await activate_recovery(
            factory,
            leg.assignment_id,
            providers={"test-car": Broken()},
            optimizers=engines(),
            now=data[6][0],
        )
        is None
    )
    async with factory() as session:
        row = await session.get(RecoveryAssignment, leg.assignment_id)
        assert row.status == "PENDING" and row.failure_code
        old = list(
            await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == root.assignment_id
                )
            )
        )
        assert all(r.released_at is None for r in old)
    assert await activate_recovery(
        factory,
        leg.assignment_id,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0] + timedelta(seconds=11),
    )


async def test_shift_expiry_requires_attention_and_never_substitutes_rider(
    database_session_factory,
):
    factory = database_session_factory
    data, incident, _ = await failed(factory)
    _, leg = await choose(factory, data, incident)
    async with factory() as session:
        shift = await session.get(OperatingShift, leg.shift_id)
    assert (
        await activate_recovery(
            factory,
            leg.assignment_id,
            providers={"test-car": LineMatrix()},
            optimizers=engines(),
            now=shift.ends_at + timedelta(seconds=1),
        )
        is None
    )
    async with factory() as session:
        row = await session.get(RecoveryAssignment, leg.assignment_id)
        assert row.shift_id == leg.shift_id and row.status in {"ATTENTION", "PENDING"}
        assert not row.activation_id


async def test_cancelled_obligation_is_resolved_without_reacceptance(database_session_factory):
    from tirodhan.modules.collection_requests.cancellation import (
        cancel_collection_request_by_customer,
    )
    from tirodhan.modules.collection_requests.models import CollectionRequest

    factory = database_session_factory
    data, incident, _ = await failed(factory)
    row, leg = await choose(factory, data, incident)
    async with factory() as session, session.begin():
        booking = await session.get(CollectionRequest, row.request_id)
        await cancel_collection_request_by_customer(
            session,
            request_id=booking.request_id,
            customer_id=booking.customer_id,
            idempotency_key=str(new_uuid7()),
            planning_lead_time_minutes=1,
            idempotency_expires_at=data[6][0] + timedelta(days=1),
        )
    assert (
        await activate_recovery(
            factory, leg.assignment_id, providers={}, optimizers={}, now=data[6][0]
        )
        is None
    )
    async with factory() as session:
        assert (await session.get(CollectionRequest, row.request_id)).status == "CANCELLED"
        assert (await session.get(RecoveryAssignment, leg.assignment_id)).status == "SUPERSEDED"
        assert (
            await session.get(RiderReplacement, row.replacement_id)
        ).failure_code == "BOOKING_CANCELLED"


async def test_reconciliation_reuses_pending_execution_without_advancing_due_past_message(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.failure_runtime import reconcile_recovery

    factory = database_session_factory
    data, incident, _ = await failed(factory)
    _, leg = await choose(factory, data, incident)
    assert await reconcile_recovery(factory, data[6][0]) == 0
    assert await claim_activation(factory, leg.assignment_id, data[6][0])


async def test_notification_resumes_per_device_and_duplicate_message_is_harmless(
    database_session_factory, database_engine
):
    import json

    from test_fleet_dispatch import Push, devices

    from tirodhan.modules.dispatch.push import PushUnavailableError
    from tirodhan.modules.routing_operations.dispatch_models import RouteRevision
    from tirodhan.modules.routing_operations.failure_models import RoutePushDelivery
    from tirodhan.modules.routing_operations.route_notifications import notify_revision

    factory = database_session_factory
    data, incident, _ = await failed(factory)
    _, leg = await choose(factory, data, incident)
    activation = await activate_recovery(
        factory,
        leg.assignment_id,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    async with factory() as session:
        revision = await session.scalar(
            select(RouteRevision).where(RouteRevision.activation_id == activation)
        )
        shift = await session.get(OperatingShift, leg.shift_id)
    await devices(factory, shift.rider_id, count=2)
    body = json.dumps(
        {"revision_id": str(revision.revision_id), "rider_id": str(shift.rider_id)}
    ).encode()
    message = str(new_uuid7())
    partial = Push(engine=database_engine, outcomes=("SENT", "PENDING"))
    with pytest.raises(PushUnavailableError):
        await notify_revision(factory, message, body, partial)
    async with factory() as session:
        assert sorted(await session.scalars(select(RoutePushDelivery.status))) == [
            "PENDING",
            "SENT",
        ]
    resumed = Push(engine=database_engine)
    await notify_revision(factory, message, body, resumed)
    assert len(resumed.calls) == 1 and len(resumed.calls[0][0]) == 1
    await notify_revision(factory, message, body, resumed)
    assert len(resumed.calls) == 1
    # A new explicit receipt-timeout wave may notify again; the same message may not.
    await notify_revision(factory, str(new_uuid7()), body, resumed)
    assert len(resumed.calls) == 2 and len(resumed.calls[1][0]) == 2
    assert resumed.calls[0][1].data() == {
        "type": "ROUTE_REVISION",
        "revision_id": str(revision.revision_id),
    }


async def test_http_decision_transport_and_revision_notification_routes(
    database_session_factory, migrated_database_url, monkeypatch
):
    import httpx
    from test_operational_api import _headers, _token_codec, _token_for

    from tirodhan.core.config import Settings
    from tirodhan.main import create_app
    from tirodhan.modules.reliability.models import InboxMessage
    from tirodhan.modules.reliability.publisher import publish_outbox_batch
    from tirodhan.workers import routing_recovery
    from tirodhan.workers.planning_worker import handle_planning_delivery

    factory = database_session_factory
    data, _ = await prepared(factory)
    assert (await run(factory, data)).status == "ACTIVATED"
    async with factory() as session:
        root = await session.scalar(select(VehicleRoute).where(VehicleRoute.status == "ACTIVE"))
        shift = await session.get(OperatingShift, root.shift_id)
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=migrated_database_url,
            routing_orchestration_enabled=True,
            routing_fleet_enabled=True,
            routing_dispatch_enabled=True,
        ),
        access_token_codec=_token_codec(),
    )
    app.state.database_session_factory = factory
    token, _ = await _token_for(factory, data[0].manager_id, roles=("MANAGER",))
    command = FailRider(
        client_command_id=new_uuid7(),
        rider_id=shift.rider_id,
        reason="RIDER_UNAVAILABLE",
        expected_route_versions={root.route_id: root.version},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://api.test"
    ) as client:
        response = await client.post(
            "/v2/manager/rider-failures",
            json=command.model_dump(mode="json"),
            headers=_headers(token),
        )
        assert response.status_code == 202, response.text
        assert (
            await client.get(
                f"/v2/manager/riders/{shift.rider_id}/recovery-state", headers=_headers(token)
            )
        ).status_code == 200

    class Publisher:
        def __init__(self):
            self.sent = []

        async def send(self, entity, message):
            self.sent.append((entity, message))

    publisher = Publisher()
    await publish_outbox_batch(
        factory,
        publisher,
        serviceability_entity="serviceability",
        batch_size=100,
        rider_notification_entity="rider",
        planning_entity="planning",
    )
    work = next(
        message
        for entity, message in publisher.sent
        if message.message_type == "RecoveryWorkRequested"
    )
    assert work.session_id.startswith("recovery:") and set(__import__("json").loads(work.body)) == {
        "work_id",
        "kind",
        "generation",
    }
    calls = []

    async def execute(actual_factory, identity, kind, settings, *, generation=None):
        calls.append(identity)
        from tirodhan.db.values import utc_now

        assert await recommend_one(
            actual_factory,
            identity,
            providers={"test-car": LineMatrix()},
            optimizers=engines(),
            now=utc_now(),
            generation=generation,
        )

    monkeypatch.setattr(routing_recovery, "execute", execute)

    class Delivery:
        message_id = work.message_id
        message_type = work.message_type
        body = work.body
        completed = 0

        async def complete(self):
            self.completed += 1

        async def abandon(self):
            raise AssertionError("unexpected abandon")

        async def dead_letter(self):
            raise AssertionError("unexpected dead letter")

    delivery = Delivery()
    await handle_planning_delivery(delivery, factory)
    await handle_planning_delivery(delivery, factory)
    assert delivery.completed == 2 and len(calls) == 1
    async with factory() as session:
        assert (
            await session.get(InboxMessage, ("routing-recovery", work.message_id))
        ).status == "PROCESSED"


async def test_competing_manager_allocations_have_one_postgresql_winner(database_session_factory):
    from test_fleet_hypothesis_concurrency import overlap

    from tirodhan.modules.payments.models import Payment

    factory = database_session_factory
    data, incident, _ = await failed(factory)
    async with factory() as session:
        row = await session.scalar(
            select(RiderReplacement).where(RiderReplacement.failure_incident_id == incident)
        )
    assert await recommend_one(
        factory,
        row.replacement_id,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    async with factory() as session:
        row = await session.get(RiderReplacement, row.replacement_id)
        candidate = await session.scalar(
            select(ReplacementCandidate)
            .where(ReplacementCandidate.evaluation_id == row.evaluation_id)
            .order_by(ReplacementCandidate.rank)
        )
        parent = await session.get(RiderFailureIncident, incident)
        payment = await session.scalar(select(Payment).where(Payment.request_id == row.request_id))
    selection = ApproveRecovery(
        client_command_id=new_uuid7(),
        incident_id=incident,
        expected_version=parent.version,
        selections=[
            RecoverySelection(
                candidate_id=candidate.candidate_id, replacement_ids=[row.replacement_id]
            )
        ],
        reason="CONCURRENT_RECOVERY",
    )

    async def one():
        return await execute_failure(
            factory, actor=data[0].manager_id, command=selection, now=data[6][0]
        )

    async def two():
        return await execute_failure(
            factory,
            actor=data[0].manager_id,
            command=selection.model_copy(update={"client_command_id": new_uuid7()}),
            now=data[6][0],
        )

    outcomes = await overlap(factory, Payment, payment.payment_id, [one, two])
    assert sum(not isinstance(r, Exception) for r in outcomes) == 1, outcomes
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RecoveryAssignment)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RecoveryAssignmentItem)
                .where(RecoveryAssignmentItem.released_at.is_(None))
            )
            == 1
        )


async def test_new_failure_hold_on_selected_resource_cannot_be_resumed_by_recovery(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.models import OperatingVehicle

    factory = database_session_factory
    data, incident, root = await failed(factory)
    _, leg = await choose(factory, data, incident)
    async with factory() as session, session.begin():
        target = await session.get(OperatingShift, leg.shift_id)
        vehicle = await session.get(OperatingVehicle, target.vehicle_id)
        vehicle.blocked_reason = "SAFETY"
        vehicle.safety_version += 1
        vehicle.version += 1
    assert (
        await activate_recovery(
            factory,
            leg.assignment_id,
            providers={"test-car": LineMatrix()},
            optimizers=engines(),
            now=data[6][0],
        )
        is None
    )
    async with factory() as session:
        attempt = await session.get(RecoveryAssignment, leg.assignment_id)
        assert (
            attempt.status == "ATTENTION" and attempt.failure_code == "CONTROLLED_RECOVERY_REQUIRED"
        )
        old = list(
            await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == root.assignment_id
                )
            )
        )
        assert all(item.released_at is None for item in old)


async def test_new_incident_history_refuses_lossy_downgrade(database_session_factory):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    factory = database_session_factory
    _, incident, _ = await failed(factory)
    with pytest.raises(DBAPIError, match="export adopted recovery work requests"):
        await asyncio.to_thread(
            command.downgrade, Config("alembic.ini"), "0035_rider_failure_recovery"
        )
    # Simulate export of notification history, then independently verify 0035's guard.
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM recovery_work_request"))
    with pytest.raises(DBAPIError, match="export adopted rider failure recovery"):
        await asyncio.to_thread(
            command.downgrade, Config("alembic.ini"), "0034_replacement_options"
        )
    async with factory() as session:
        assert (await session.get(RiderFailureIncident, incident)).status == "OPEN"


@pytest.mark.parametrize("boundary", ["two_session", "after_preflight", "after_reconcile"])
async def test_cancellation_racing_recovery_never_mints_empty_activation_or_restores_booking(
    database_session_factory, monkeypatch, boundary
):
    from test_fleet_hypothesis_concurrency import overlap

    from tirodhan.modules.collection_requests.cancellation import (
        cancel_collection_request_by_customer,
    )
    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.payments.models import Payment
    from tirodhan.modules.planning.models import PickupExecution
    from tirodhan.modules.routing_operations import failure_execution
    from tirodhan.modules.routing_operations.dispatch_models import RouteTask
    from tirodhan.modules.routing_operations.failure_runtime import reconcile_recovery

    factory = database_session_factory
    data, incident, _ = await failed(factory)
    row, leg = await choose(factory, data, incident)
    async with factory() as session:
        booking = await session.get(CollectionRequest, row.request_id)
        payment = await session.scalar(select(Payment).where(Payment.request_id == row.request_id))
    cancel_key = str(new_uuid7())

    async def cancel():
        async with factory() as session, session.begin():
            return await cancel_collection_request_by_customer(
                session,
                request_id=row.request_id,
                customer_id=booking.customer_id,
                idempotency_key=cancel_key,
                planning_lead_time_minutes=1,
                idempotency_expires_at=data[6][0] + timedelta(days=1),
            )

    async def activate():
        return await activate_recovery(
            factory,
            leg.assignment_id,
            providers={"test-car": LineMatrix()},
            optimizers=engines(),
            now=data[6][0],
        )

    if boundary == "two_session":
        await overlap(factory, Payment, payment.payment_id, [activate, cancel])
    else:
        actual = failure_execution.resolve_terminal_obligations

        async def interleave(*args, **kwargs):
            result = await actual(*args, **kwargs)
            await cancel()
            if boundary == "after_reconcile":
                await reconcile_recovery(factory, data[6][0])
            return result

        monkeypatch.setattr(failure_execution, "resolve_terminal_obligations", interleave)
        assert await activate() is None
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(RecoveryWitness)) == 0
    await cancel()  # exact replay or retry a SERIALIZABLE loser
    await reconcile_recovery(factory, data[6][0])
    async with factory() as session:
        assert (await session.get(CollectionRequest, row.request_id)).status == "CANCELLED"
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == row.request_id)
        )
        assert not await session.scalar(
            select(RouteTask.task_id)
            .where(
                RouteTask.pickup_execution_id == pickup.pickup_execution_id,
                RouteTask.state.in_(("PENDING", "STARTED")),
            )
            .limit(1)
        )
        current = await session.get(RiderReplacement, row.replacement_id)
        assert current.status in ("COMPLETED", "REJECTED")


async def test_authority_lock_wait_rechecks_live_position_freshness(
    database_session_factory, monkeypatch
):
    from types import SimpleNamespace

    import test_operational_orchestration as inputs
    import test_regional_parallel as planning

    from tirodhan.modules.routing_operations import failure_execution

    original_policy = inputs.policy

    def fresh_positions_policy(*args, **kwargs):
        configured = original_policy(*args, **kwargs)
        return configured.model_copy(
            update={"telemetry": configured.telemetry.model_copy(update={"max_age_seconds": 30})}
        )

    # Configure both immutable authorities through the normal fixture commands.
    monkeypatch.setattr(inputs, "policy", fresh_positions_policy)
    monkeypatch.setattr(planning, "policy", fresh_positions_policy)
    factory = database_session_factory
    data, incident, root = await failed(factory)
    _, leg = await choose(factory, data, incident)
    from tirodhan.modules.routing_operations.orchestration_models import OperatingRegion

    async with factory() as session:
        config_region = await session.get(OperatingRegion, data[7])
        assert config_region.policy["fleet"]["telemetry"]["max_age_seconds"] == 30
    elapsed = [0.0]
    actual = failure_execution.lock_resources

    async def wait_for_authority(*args, **kwargs):
        await actual(*args, **kwargs)
        elapsed[0] = 45.0  # Below the 120s lease, beyond the position freshness bound.

    monkeypatch.setattr(failure_execution, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(failure_execution, "lock_resources", wait_for_authority)
    result = await activate_recovery(
        factory,
        leg.assignment_id,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    assert elapsed[0] == 45
    assert result is None
    async with factory() as session:
        current = await session.get(RecoveryAssignment, leg.assignment_id)
        assert current.status == "ATTENTION"
        assert current.failure_code == "RESOURCE_AUTHORIZATION_STALE"
        assert await session.scalar(select(func.count()).select_from(RecoveryWitness)) == 0
        old = list(
            await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == root.assignment_id
                )
            )
        )
        assert old and all(item.released_at is None for item in old)


async def test_incident_receipt_tracks_selected_resource_current_revision(
    database_session_factory,
):
    from types import SimpleNamespace

    from test_operational_orchestration import operational

    from tirodhan.api.routes.routing_recovery import incident_view
    from tirodhan.modules.routing_operations.dispatch_models import RouteRevision
    from tirodhan.modules.routing_operations.operational_commands import AcknowledgeRoute

    factory = database_session_factory
    data, incident, original = await failed(factory)
    _, first = await choose(factory, data, incident)
    first_activation = await activate_recovery(
        factory,
        first.assignment_id,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    assert first_activation
    _, second = await choose(factory, data, incident)
    second_activation = await activate_recovery(
        factory,
        second.assignment_id,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    assert second_activation and first.shift_id == second.shift_id
    async with factory() as session:
        latest = await session.scalar(
            select(RouteRevision).where(RouteRevision.activation_id == second_activation)
        )
        receiver = await session.get(OperatingShift, second.shift_id)
    await operational(
        factory,
        receiver.rider_id,
        AcknowledgeRoute(client_command_id=new_uuid7(), revision_id=latest.revision_id),
        data[6][0] + timedelta(seconds=2),
    )
    view = await incident_view(
        incident, SimpleNamespace(user_id=data[0].manager_id), factory, limit=50
    )
    assert len(view["assignments"]) == 2
    assert all(v["acknowledged"] for v in view["assignments"]), {
        "assignments": view["assignments"],
        "acknowledged_revision": str(latest.revision_id),
    }
    assert all(v["revision_id"] == latest.revision_id for v in view["assignments"])
    prior = next(v for v in view["assignments"] if v["assignment_id"] == first.assignment_id)
    assert prior["activation_revision_id"] != latest.revision_id
    from tirodhan.modules.routing_operations.failure_commands import recovery_revisions

    async with factory() as session:
        unrelated = await session.scalar(
            select(RouteRevision).where(
                RouteRevision.route_id == original.route_id, RouteRevision.version == 1
            )
        )
        selected_revisions = await recovery_revisions(
            session, unrelated.activation_id, second.shift_id
        )
        assert all(revision.route_id != original.route_id for revision in selected_revisions)
