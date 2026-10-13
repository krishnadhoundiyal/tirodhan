from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from alembic import command as migrations
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_payment_preparation import assert_readiness_schema
from test_routing_operations import (
    Matrix,
    _headers,
    _token_codec,
    _token_for,
    process,
    requested,
    setup,
)

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem, RiderAvailability
from tirodhan.modules.dispatch.service import assign_group_manually
from tirodhan.modules.evidence.models import EvidenceCapture, HandoverEvidenceLink
from tirodhan.modules.handovers.service import HandoverStateInconsistentError, record_handover
from tirodhan.modules.identity.models import UserRole
from tirodhan.modules.operations.service import ReassignmentNotFoundError, reassign_outstanding_work
from tirodhan.modules.payments.models import Payment, PaymentAttempt, Refund
from tirodhan.modules.pickups.models import PickupAttempt
from tirodhan.modules.pickups.service import (
    AssignmentNotStartableError,
    PickupNotAttemptableError,
    record_pickup_attempt,
    start_assignment,
)
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.planning.routing.contracts import OptimizerEngine
from tirodhan.modules.reliability.models import IdempotencyRecord, OutboxEvent
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.routing_operations.authority import (
    ActivationCommand,
    RouteAuthorityError,
    activate_proposal,
)
from tirodhan.modules.routing_operations.dispatch_models import (
    RouteActivation,
    RouteDischargeProof,
    RouteRevision,
    RouteRevisionStop,
    RouteTask,
    VehicleRoute,
)
from tirodhan.modules.routing_operations.execution import (
    DischargeTask,
    PickupTask,
    StartTask,
    current_tasks,
    execute_task,
)
from tirodhan.modules.routing_operations.models import (
    CargoVehicleObservation,
    RoutingManifest,
)
from tirodhan.modules.routing_operations.recovery import RecoveryCommand, recover_route
from tirodhan.modules.routing_operations.snapshot import build_problem, capture_snapshot

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def prepared(factory, *, advance=False, unplanned=False, position=(77.2090, 28.6139)):
    fixture, vehicle, shift, point, ids, config, execute = await setup(factory, position=position)
    now = config.epoch
    config = config.model_copy(update={"max_position_age_seconds": 20000})
    async with factory() as session, session.begin():
        for request in await session.scalars(select(CollectionRequest)):
            payment = Payment(
                request_id=request.request_id,
                amount_minor=request.quoted_amount_minor,
                currency=request.currency,
                status="SUCCEEDED",
                succeeded_at=now,
            )
            session.add(payment)
            await session.flush([payment])
            attempt = PaymentAttempt(
                payment_id=payment.payment_id,
                provider="fake",
                provider_payment_id=f"capture-{payment.payment_id}",
                provider_idempotency_key=f"attempt-{payment.payment_id}",
                status="SUCCEEDED",
                completed_at=now,
            )
            session.add(attempt)
            await session.flush([attempt])
            payment.successful_attempt_id = attempt.payment_attempt_id
            if advance:
                request.status, request.planning_batch_id = "ACCEPTED", None
        point = await session.get(type(point), point.receiving_point_id)
        point.official_identifier = "test-kiosk-42"
        if unplanned:
            from sqlalchemy import delete

            await session.execute(delete(PickupExecution))
    run_id = await requested(factory, fixture, config)
    run = await process(factory, run_id, config, clock=lambda: now)
    assert run.status == "SUCCEEDED", run.failure_code
    command = ActivationCommand(
        client_command_id=new_uuid7(), run_id=run_id, engine=OptimizerEngine.VROOM
    )
    return fixture, vehicle, point, ids, config, execute, command


async def activate(factory, fixture, command, now):
    return await activate_proposal(
        factory,
        manager_id=fixture.manager_id,
        command=command,
        expires_at=now + timedelta(days=1),
        now=now,
    )


async def root_revision(factory):
    async with factory() as session:
        root = await session.scalar(select(VehicleRoute).order_by(VehicleRoute.created_at.desc()))
        revision = await session.scalar(
            select(RouteRevision).where(
                RouteRevision.route_id == root.route_id, RouteRevision.version == root.version
            )
        )
        tasks = await current_tasks(session, root)
        return root, revision, tasks


async def execute(factory, rider, command, now):
    return await execute_task(
        factory, rider_id=rider, command=command, expires_at=now + timedelta(days=1), now=now
    )


async def add_booking(factory, config, point, original_id, manager_id, *, road=False):
    from geoalchemy2.elements import WKTElement

    from tirodhan.modules.collection_requests.models import CollectionRequestItem
    from tirodhan.modules.serviceability.models import ServiceabilityContext

    async with factory() as session, session.begin():
        original = await session.get(CollectionRequest, original_id)
        context = ServiceabilityContext(
            user_id=original.customer_id,
            address_snapshot_encrypted=original.pickup_address_snapshot_encrypted,
            location=original.pickup_location,
            cell_id="additional-cell",
            status="SERVICEABLE",
            expires_at=config.horizon_end,
            resolved_at=config.epoch,
        )
        session.add(context)
        await session.flush([context])
        booking = CollectionRequest(
            client_request_id=new_uuid7(),
            customer_id=original.customer_id,
            serviceability_context_id=context.serviceability_context_id,
            pickup_address_snapshot_encrypted=original.pickup_address_snapshot_encrypted,
            pickup_location=WKTElement("POINT(77.2105 28.6305)", srid=4326)
            if road
            else original.pickup_location,
            cell_id="additional-cell",
            slot_start=config.epoch + timedelta(minutes=105),
            slot_end=config.epoch + timedelta(minutes=135),
            quoted_amount_minor=500,
            currency="INR",
            status="ACCEPTED",
            payment_expires_at=config.epoch + timedelta(minutes=20),
            accepted_at=config.epoch,
        )
        session.add(booking)
        await session.flush([booking])
        session.add(
            CollectionRequestItem(
                request_id=booking.request_id,
                item_category_code="TEST",
                quoted_line_amount_minor=500,
                currency="INR",
            )
        )
        session.add(
            RoutingManifest(
                request_id=booking.request_id,
                mass_g=2000,
                volume_ml=2000,
                pickup_seconds=30,
                discharge_seconds=30,
                discharge_deadline=config.epoch + timedelta(hours=3),
                receiving_point_id=point.receiving_point_id,
                recorded_by=manager_id,
                created_at=config.epoch,
            )
        )
        await session.flush()
        payment = Payment(
            request_id=booking.request_id, amount_minor=500, currency="INR", status="SUCCEEDED"
        )
        session.add(payment)
        await session.flush([payment])
        attempt = PaymentAttempt(
            payment_id=payment.payment_id,
            provider="fake",
            provider_idempotency_key=f"attempt-{payment.payment_id}",
            provider_payment_id=f"capture-{payment.payment_id}",
            status="SUCCEEDED",
            completed_at=config.epoch,
        )
        session.add(attempt)
        await session.flush([attempt])
        payment.successful_attempt_id = attempt.payment_attempt_id
        return booking.request_id


async def collect_first(factory, fixture, config):
    root, revision, tasks = await root_revision(factory)
    task = tasks[0]
    async with factory() as session:
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        request = await session.get(CollectionRequest, pickup.request_id)
    now = request.slot_start
    await execute(
        factory,
        fixture.rider_ids[0],
        StartTask(
            client_command_id=new_uuid7(), task_id=task.task_id, revision_id=revision.revision_id
        ),
        now,
    )
    command = PickupTask(
        client_command_id=new_uuid7(),
        task_id=task.task_id,
        revision_id=revision.revision_id,
        outcome="COLLECTED",
    )
    attempt = await execute(factory, fixture.rider_ids[0], command, now + timedelta(seconds=30))
    return root, revision, task, command, attempt, now + timedelta(seconds=30)


async def test_activation_cross_group_and_advance_rights(database_session_factory):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f, advance=True)
    result = await activate(f, fixture, command, config.epoch)
    assert await activate(f, fixture, command, config.epoch) == result
    async with f() as session:
        assignment = await session.scalar(select(RiderAssignment))
        assert assignment.source == "COMMITTED_DISPATCH" and assignment.collection_group_id is None
        assert assignment.started_at == config.epoch
        assert (await session.get(RiderAvailability, fixture.rider_ids[0])).work_state == "BUSY"
        assert {r.status for r in await session.scalars(select(CollectionRequest))} == {"ACCEPTED"}
        assert await session.scalar(select(func.count()).select_from(RouteTask)) == 4
        assert await session.scalar(select(func.count()).select_from(RouteRevision)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "VehicleRouteRevised")
            )
            == 1
        )
    with pytest.raises(IdempotencyKeyConflictError):
        await activate(
            f,
            fixture,
            command.model_copy(update={"engine": OptimizerEngine.OR_TOOLS}),
            config.epoch,
        )


@pytest.mark.parametrize("change", ["position", "role", "ownership", "payment", "slot"])
async def test_stale_activation_has_no_effect(database_session_factory, change):
    f = database_session_factory
    fixture, vehicle, point, ids, config, execute_input, command = await prepared(f)
    if change == "position":
        from tirodhan.modules.routing_operations.commands import (
            PositionReport,
            execute_resource_command,
        )

        await execute_resource_command(
            f,
            manager_id=fixture.manager_id,
            expires_at=config.epoch + timedelta(days=1),
            now=config.epoch + timedelta(seconds=1),
            command=PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicle,
                longitude=77.209,
                latitude=28.6139,
                observed_at=config.epoch + timedelta(seconds=1),
            ),
        )
    else:
        async with f() as session, session.begin():
            if change == "role":
                await session.execute(
                    update(UserRole)
                    .where(UserRole.user_id == fixture.rider_ids[0])
                    .values(revoked_at=utc_now())
                )
            elif change == "payment":
                await session.execute(update(Payment).values(status="CANCELLED"))
            elif change == "slot":
                # A committed input changes after the validated snapshot.
                request = await session.get(CollectionRequest, ids[0])
                request.slot_start += timedelta(seconds=1)
            else:
                pass
        if change == "ownership":
            await assign_group_manually(
                f,
                collection_group_id=fixture.group_ids[0],
                rider_id=fixture.rider_ids[1],
                manager_user_id=fixture.manager_id,
            )
    with pytest.raises(RouteAuthorityError):
        await activate(f, fixture, command, config.epoch + timedelta(seconds=1))
    async with f() as session:
        assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 0
        assert await session.scalar(select(func.count()).select_from(RouteRevision)) == 0


async def test_concurrent_activations_have_one_winner(database_session_factory):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    other = command.model_copy(
        update={"client_command_id": new_uuid7(), "engine": OptimizerEngine.OR_TOOLS}
    )
    results = await asyncio.wait_for(
        asyncio.gather(
            activate(f, fixture, command, config.epoch),
            activate(f, fixture, other, config.epoch),
            return_exceptions=True,
        ),
        10,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1, results
    assert isinstance(next(r for r in results if isinstance(r, Exception)), RouteAuthorityError)
    async with f() as session:
        assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 1
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1
        assert await session.scalar(select(func.count()).select_from(RiderAssignmentItem)) == 2


async def test_collected_cargo_keeps_busy_and_cannot_use_legacy_paths(database_session_factory):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    await activate(f, fixture, command, config.epoch)
    root, revision, task, pickup_command, attempt, now = await collect_first(f, fixture, config)
    assert await execute(f, fixture.rider_ids[0], pickup_command, now) == attempt
    async with f() as session:
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        assert pickup.status == "COLLECTED"
        cargo = await session.get(CargoVehicleObservation, pickup.request_id)
        assert cargo.vehicle_id == vehicle and cargo.pickup_attempt_id == attempt
        assert (await session.get(RiderAvailability, fixture.rider_ids[0])).work_state == "BUSY"
        assert (await session.get(RiderAssignment, root.assignment_id)).status == "ACTIVE"
    with pytest.raises(PickupNotAttemptableError):
        await record_pickup_attempt(
            f,
            pickup_execution_id=task.pickup_execution_id,
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome="COLLECTED",
        )
    with pytest.raises(AssignmentNotStartableError):
        await start_assignment(f, assignment_id=root.assignment_id, rider_id=fixture.rider_ids[0])
    with pytest.raises(HandoverStateInconsistentError):
        await record_handover(
            f,
            client_handover_id=new_uuid7(),
            rider_id=fixture.rider_ids[0],
            receiving_point_id=point.receiving_point_id,
            pickup_execution_ids=[task.pickup_execution_id],
            observed_location=None,
            idempotency_expires_at=now + timedelta(days=1),
        )
    with pytest.raises(ReassignmentNotFoundError):
        await reassign_outstanding_work(
            f,
            predecessor_assignment_id=root.assignment_id,
            replacement_rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
            client_reassignment_id=new_uuid7(),
            idempotency_expires_at=now + timedelta(days=1),
        )


async def test_recovery_retains_collected_history_releases_only_uncollected(
    database_session_factory,
):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    await activate(f, fixture, command, config.epoch)
    root, revision, task, _, attempt, now = await collect_first(f, fixture, config)
    recovery = RecoveryCommand(
        client_command_id=new_uuid7(),
        route_id=root.route_id,
        revision_id=revision.revision_id,
        reason_code="VEHICLE_UNAVAILABLE",
    )
    result = await recover_route(
        f,
        manager_id=fixture.manager_id,
        command=recovery,
        expires_at=now + timedelta(days=1),
        now=now,
    )
    assert (
        await recover_route(
            f,
            manager_id=fixture.manager_id,
            command=recovery,
            expires_at=now + timedelta(days=1),
            now=now,
        )
        == result
    )
    async with f() as session:
        root = await session.get(VehicleRoute, root.route_id)
        assert root.status == "RECOVERY_REQUIRED"
        collected_item = await session.get(
            RiderAssignmentItem, (root.assignment_id, task.pickup_execution_id)
        )
        assert collected_item.released_at is None
        assert (await session.get(PickupAttempt, attempt)).outcome == "COLLECTED"
        pickups = list(await session.scalars(select(PickupExecution)))
        assert sorted(p.status for p in pickups) == ["COLLECTED", "PENDING_ASSIGNMENT"]
        assert (await session.get(RiderAvailability, fixture.rider_ids[0])).work_state == "BUSY"
        remaining = await current_tasks(session, root)
        assert len(remaining) == 1 and remaining[0].kind == "DISCHARGE"
        discharge = remaining[0]
    await execute(
        f,
        fixture.rider_ids[0],
        StartTask(
            client_command_id=new_uuid7(),
            task_id=discharge.task_id,
            revision_id=revision.revision_id,
        ),
        now,
    )
    proof = DischargeTask(
        client_command_id=new_uuid7(),
        task_id=discharge.task_id,
        revision_id=revision.revision_id,
        scanned_pickup_id=task.pickup_execution_id,
        scanned_receiving_identifier="test-kiosk-42",
        client_capture_id=new_uuid7(),
        captured_at=now,
        longitude=77.209,
        latitude=28.6139,
    )
    rejected = proof.model_copy(
        update={
            "client_command_id": new_uuid7(),
            "client_capture_id": new_uuid7(),
            "longitude": 78.0,
        }
    )
    rejected_id = await execute(f, fixture.rider_ids[0], rejected, now)
    assert await execute(f, fixture.rider_ids[0], rejected, now) == rejected_id
    from tirodhan.modules.handovers.models import HandoverEvent

    async with f() as session:
        assert (await session.get(HandoverEvent, rejected_id)).status == "REJECTED"
        assert (await session.get(RouteTask, discharge.task_id)).state == "STARTED"
        assert (await session.get(RiderAvailability, fixture.rider_ids[0])).work_state == "BUSY"
    event_id = await execute(f, fixture.rider_ids[0], proof, now)
    assert await execute(f, fixture.rider_ids[0], proof, now) == event_id
    async with f() as session:
        assert (await session.get(VehicleRoute, root.route_id)).status == "CLOSED"
        assert (await session.get(RiderAvailability, fixture.rider_ids[0])).work_state == "IDLE"
        assert await session.get(RouteDischargeProof, event_id) is not None
        assert await session.scalar(select(func.count()).select_from(HandoverEvidenceLink)) == 2
        assert await session.scalar(select(func.count()).select_from(EvidenceCapture)) == 2


async def test_revision_preserves_started_prefix_and_history(database_session_factory):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    await activate(f, fixture, command, config.epoch)
    root, revision, tasks = await root_revision(f)
    now = config.epoch + timedelta(minutes=30)
    config = config.model_copy(update={"max_position_age_seconds": 20000})
    await execute(
        f,
        fixture.rider_ids[0],
        StartTask(
            client_command_id=new_uuid7(),
            task_id=tasks[0].task_id,
            revision_id=revision.revision_id,
        ),
        now,
    )
    snapshot = await capture_snapshot(f, config)
    problem = await build_problem(snapshot, config, new_uuid7(), {"test-car": Matrix()}, now=now)
    assert problem.vehicles[0].protected_prefix == (tasks[0].stop_id,)
    run_id = await requested(f, fixture, config)
    run = await process(f, run_id, config, clock=lambda: now)
    assert run.status == "SUCCEEDED", run.results
    await activate(
        f,
        fixture,
        ActivationCommand(
            client_command_id=new_uuid7(), run_id=run_id, engine=OptimizerEngine.OR_TOOLS
        ),
        now,
    )
    current_root, new_revision, new_tasks = await root_revision(f)
    assert current_root.route_id == root.route_id and current_root.version == 2
    assert new_revision.supersedes_revision_id == revision.revision_id
    assert new_tasks[0].task_id == tasks[0].task_id and new_tasks[0].state == "STARTED"
    with pytest.raises(RouteAuthorityError, match="ROUTE_REVISION_STALE"):
        await execute(
            f,
            fixture.rider_ids[0],
            PickupTask(
                client_command_id=new_uuid7(),
                task_id=tasks[1].task_id,
                revision_id=revision.revision_id,
                outcome="COLLECTED",
            ),
            now,
        )
    async with f() as session:
        assert await session.scalar(select(func.count()).select_from(RouteRevisionStop)) == 8
        assert await session.scalar(select(func.count()).select_from(RouteTask)) == 4


async def test_cancellation_detaches_advance_work_even_with_revoked_rider(database_session_factory):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f, advance=True)
    await activate(f, fixture, command, config.epoch)
    async with f() as session, session.begin():
        request = await session.get(CollectionRequest, ids[0])
        customer = request.customer_id
        await session.execute(
            update(UserRole)
            .where(UserRole.user_id == fixture.rider_ids[0])
            .values(revoked_at=utc_now())
        )
    from tirodhan.modules.collection_requests.cancellation import (
        cancel_collection_request_by_customer,
    )

    for key in ("cancel-one", "cancel-one", "cancel-two"):
        async with f() as session, session.begin():
            result = await cancel_collection_request_by_customer(
                session,
                request_id=ids[0],
                customer_id=customer,
                idempotency_key=key,
                planning_lead_time_minutes=1,
                idempotency_expires_at=config.epoch + timedelta(days=1),
            )
            assert result.status == "CANCELLED"
    async with f() as session:
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == ids[0])
        )
        item = await session.scalar(
            select(RiderAssignmentItem).where(
                RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id
            )
        )
        assert await session.scalar(select(func.count()).select_from(Refund)) == 1
        assert item.release_reason_code == "CANCELLED" and item.released_at is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RouteTask)
                .where(
                    RouteTask.pickup_execution_id == pickup.pickup_execution_id,
                    RouteTask.state == "CANCELLED",
                )
            )
            == 2
        )


async def test_activation_rollback_has_no_ownership_or_idempotency(
    database_session_factory, monkeypatch
):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    from tirodhan.modules.routing_operations import authority

    original = authority.append_outbox_event

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated before commit")

    monkeypatch.setattr(authority, "append_outbox_event", crash)
    with pytest.raises(RuntimeError):
        await activate(f, fixture, command, config.epoch)
    async with f() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 0
        assert await session.scalar(select(func.count()).select_from(RouteTask)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.scope.like("routing.activate:%"))
            )
            == 0
        )
    monkeypatch.setattr(authority, "append_outbox_event", original)
    await activate(f, fixture, command, config.epoch)


async def test_history_is_immutable_and_downgrade_refuses_loss(database_session_factory):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    await activate(f, fixture, command, config.epoch)
    async with f() as session:
        with pytest.raises(IntegrityError):
            await session.execute(text("UPDATE route_revision SET version = 99"))
        await session.rollback()
        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE vehicle_route SET assignment_id = :id"), {"id": new_uuid7()}
            )
        await session.rollback()
        with pytest.raises(IntegrityError):
            await session.execute(text("UPDATE route_task SET state = 'COMPLETED'"))
        await session.rollback()
        with pytest.raises(IntegrityError):
            await session.execute(text("UPDATE operating_shift SET resource_kind = 'FLEXIBLE'"))
        await session.rollback()
    with pytest.raises(DBAPIError, match="0025 downgrade blocked"):
        await asyncio.to_thread(
            migrations.downgrade, Config("alembic.ini"), "0024_routing_operational_inputs"
        )
    async with f() as session:
        assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 1
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        )


async def test_migration_v1_history_round_trip_and_no_new_drift(database_session_factory):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    await asyncio.to_thread(
        migrations.downgrade, Config("alembic.ini"), "0024_routing_operational_inputs"
    )
    await asyncio.to_thread(migrations.upgrade, Config("alembic.ini"), "head")
    async with f() as session:
        assert await session.scalar(select(func.count()).select_from(PickupExecution)) == 2
        assert await session.scalar(select(func.count()).select_from(Payment)) == 2
        connection = await session.connection()
        await connection.run_sync(assert_readiness_schema)


async def test_dispatch_api_default_closed_and_live_roles(database_session_factory):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    manager_token, _ = await _token_for(f, fixture.manager_id, roles=("MANAGER",))
    rider_token, _ = await _token_for(f, fixture.rider_ids[0], roles=("RIDER",))
    app = create_app(
        Settings(_env_file=None, environment="test"), access_token_codec=_token_codec()
    )
    app.state.database_session_factory = f
    app.state.database_session_factory = f
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        assert (
            await client.post(
                "/v2/manager/routes/activate",
                json=command.model_dump(mode="json"),
                headers=_headers(manager_token),
            )
        ).status_code == 404
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            routing_dispatch_enabled=True,
            command_idempotency_ttl_seconds=86400,
        ),
        access_token_codec=_token_codec(),
    )
    app.state.database_session_factory = f
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        assert (
            await client.post(
                "/v2/manager/routes/activate",
                json=command.model_dump(mode="json"),
                headers=_headers(rider_token),
            )
        ).status_code == 403
        result = await client.post(
            "/v2/manager/routes/activate",
            json=command.model_dump(mode="json"),
            headers=_headers(manager_token),
        )
        assert result.status_code == 200, result.text
        committed = result.json()["routes"][0]
        manager_view = await client.get(
            f"/v2/manager/routes/{committed['route_id']}", headers=_headers(manager_token)
        )
        assert manager_view.status_code == 200
        assert manager_view.json()["revision_id"] == committed["revision_id"]
        view = await client.get("/v2/rider/route", headers=_headers(rider_token))
        assert view.status_code == 200 and len(view.json()["tasks"]) == 4
        assert "problem" not in view.json() and "coordinates" not in view.text


async def test_new_v2_work_has_no_synthetic_groups_and_never_reenters_freeze(
    database_session_factory,
):
    from tirodhan.modules.planning.service import PlanningWorkUnit, freeze_planning_batch

    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(
        f, advance=True, unplanned=True
    )
    await activate(f, fixture, command, config.epoch)
    async with f() as session:
        pickups = list(await session.scalars(select(PickupExecution)))
        assert len(pickups) == 2 and all(p.collection_group_id is None for p in pickups)
        request = await session.get(CollectionRequest, ids[0])
    frozen = await freeze_planning_batch(
        f,
        PlanningWorkUnit(request.cell_id, request.slot_start, request.slot_end),
        lead_time_minutes=1,
        max_attempts=3,
        compaction_distance_m=100,
        max_group_requests=4,
        now=request.slot_start,
    )
    assert frozen.transitioned_request_count == 0
    async with f() as session:
        assert (await session.get(CollectionRequest, ids[0])).status == "ACCEPTED"


async def test_activation_rechecks_time_feasibility(database_session_factory):
    from tirodhan.modules.planning.routing.validation import RoutingValidationError

    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    with pytest.raises(RoutingValidationError):
        await activate(f, fixture, command, config.epoch + timedelta(hours=2))
    async with f() as session:
        assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 0


async def test_one_solver_failure_preserves_other_validated_dispatch_candidate(
    database_session_factory,
):
    from test_routing_operations import engines

    from tirodhan.modules.planning.routing.execution import RoutingAdapterError

    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)

    class Unavailable:
        async def propose(self, *args, **kwargs):
            raise RoutingAdapterError("ENGINE_UNAVAILABLE")

    optimizers = engines()
    optimizers[OptimizerEngine.VROOM] = Unavailable()
    run = await process(
        f,
        await requested(f, fixture, config),
        config,
        optimizers=optimizers,
        clock=lambda: config.epoch,
    )
    assert run.status == "FAILED" and run.failure_code == "ENGINE_EVALUATION_FAILED"
    for engine in (OptimizerEngine.VROOM, OptimizerEngine.OR_TOOLS):
        activation = ActivationCommand(
            client_command_id=new_uuid7(), run_id=run.run_id, engine=engine
        )
        if engine == OptimizerEngine.VROOM:
            with pytest.raises(RouteAuthorityError, match="VALIDATED_ENGINE_RESULT_REQUIRED"):
                await activate(f, fixture, activation, config.epoch)
        else:
            await activate(f, fixture, activation, config.epoch)
    async with f() as session:
        assert (await session.scalar(select(RouteActivation))).engine == "OR_TOOLS"
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1


async def test_unused_future_shift_does_not_block_active_committed_dispatch(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.commands import (
        PositionReport,
        ShiftOpen,
        VehicleCreate,
    )

    f = database_session_factory
    fixture, vehicle, point, ids, config, execute_input, command = await prepared(f)
    future = await execute_input(
        VehicleCreate(
            client_command_id=new_uuid7(),
            mass_capacity_g=1000,
            volume_capacity_ml=1000,
            profile_version="test-car",
            end_receiving_point_id=point.receiving_point_id,
        )
    )
    await execute_input(
        ShiftOpen(
            client_command_id=new_uuid7(),
            vehicle_id=future,
            rider_id=fixture.rider_ids[1],
            resource_kind="COMMITTED",
            commitment_id=new_uuid7(),
            starts_at=config.epoch + timedelta(hours=2),
            ends_at=config.horizon_end,
        )
    )
    await execute_input(
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=future,
            longitude=77.209,
            latitude=28.6139,
            observed_at=config.epoch,
        )
    )
    run = await process(f, await requested(f, fixture, config), config, clock=lambda: config.epoch)
    assert run.status == "SUCCEEDED", run.results
    await activate(
        f,
        fixture,
        ActivationCommand(
            client_command_id=new_uuid7(), run_id=run.run_id, engine=OptimizerEngine.VROOM
        ),
        config.epoch,
    )
    async with f() as session:
        assert await session.scalar(select(func.count()).select_from(VehicleRoute)) == 1
        assert (await session.scalar(select(VehicleRoute))).vehicle_id == vehicle
        assert (await session.get(RiderAvailability, fixture.rider_ids[1])).work_state == "IDLE"


async def test_loaded_busy_vehicle_receives_new_booking_without_resetting_cargo(
    database_session_factory,
):
    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f, advance=True)
    await activate(f, fixture, command, config.epoch)
    root, revision, task, _, attempt, now = await collect_first(f, fixture, config)
    new_id = await add_booking(f, config, point, ids[0], fixture.manager_id)
    run = await process(f, await requested(f, fixture, config), config, clock=lambda: now)
    assert run.status == "SUCCEEDED", run.results
    await activate(
        f,
        fixture,
        ActivationCommand(
            client_command_id=new_uuid7(), run_id=run.run_id, engine=OptimizerEngine.OR_TOOLS
        ),
        now,
    )
    current, revision, tasks = await root_revision(f)
    assert current.route_id == root.route_id and current.assignment_id == root.assignment_id
    assert current.version == 2 and len(tasks) == 5
    async with f() as session:
        assert (await session.get(CargoVehicleObservation, ids[0])).pickup_attempt_id == attempt
        assert (await session.get(RiderAvailability, fixture.rider_ids[0])).work_state == "BUSY"
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == new_id)
        )
        assert pickup.status == "ASSIGNED" and pickup.collection_group_id is None
        assert await session.scalar(select(func.count()).select_from(RiderAssignmentItem)) == 3


async def test_v1_assignment_races_v2_activation_with_one_owner(database_session_factory):
    from tirodhan.modules.dispatch.service import DispatchError

    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    results = await asyncio.wait_for(
        asyncio.gather(
            activate(f, fixture, command, config.epoch),
            assign_group_manually(
                f,
                collection_group_id=fixture.group_ids[0],
                rider_id=fixture.rider_ids[1],
                manager_user_id=fixture.manager_id,
            ),
            return_exceptions=True,
        ),
        12,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1, results
    assert isinstance(
        next(r for r in results if isinstance(r, Exception)), (RouteAuthorityError, DispatchError)
    )
    async with f() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1


async def test_position_recorder_fk_does_not_cycle_with_dispatch_authority_locks(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.models import OperatingVehicle, VehiclePosition

    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    # One human can hold RIDER and MANAGER. A position's recorder FK can target
    # the rider identity whose authority the waiting dispatch transaction locks.
    await _token_for(f, fixture.rider_ids[0], roles=("RIDER", "MANAGER"))
    task = None
    try:
        async with f() as session, session.begin():
            resource = await session.scalar(
                select(OperatingVehicle)
                .where(OperatingVehicle.vehicle_id == vehicle)
                .with_for_update()
            )
            xid = await session.scalar(text("SELECT txid_current()::text"))
            task = asyncio.create_task(activate(f, fixture, command, config.epoch))

            async def wait_for_vehicle_lock():
                for _ in range(500):
                    async with f() as monitor:
                        waiting = await monitor.scalar(
                            text(
                                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE NOT granted "
                                "AND transactionid::text=:xid)"
                            ),
                            {"xid": xid},
                        )
                    if waiting:
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("dispatch did not wait for the position writer")

            await asyncio.wait_for(wait_for_vehicle_lock(), 7)
            resource.version += 1
            session.add(
                VehiclePosition(
                    vehicle_id=vehicle,
                    revision=resource.version,
                    location="SRID=4326;POINT(77.209 28.6139)",
                    observed_at=config.epoch + timedelta(seconds=1),
                    recorded_by=fixture.rider_ids[0],
                    created_at=config.epoch,
                )
            )
            # Must finish while dispatch still holds user/role/profile authority locks.
            await asyncio.wait_for(session.flush(), 2)
        result = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 7))[0]
        assert isinstance(result, RouteAuthorityError)
        assert str(result) in ("SOURCE_CHANGED", "RETRYABLE_TRANSACTION_CONFLICT")
        async with f() as session:
            assert await session.scalar(select(func.count()).select_from(VehiclePosition)) == 2
            assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_races_activation_without_stranding_ownership(database_session_factory):
    from tirodhan.modules.collection_requests.cancellation import (
        cancel_collection_request_by_customer,
    )

    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f, advance=True)
    async with f() as session:
        customer = (await session.get(CollectionRequest, ids[0])).customer_id

    async def cancel():
        async with f() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout = '5s'"))
            return await cancel_collection_request_by_customer(
                session,
                request_id=ids[0],
                customer_id=customer,
                idempotency_key="race",
                planning_lead_time_minutes=1,
                idempotency_expires_at=config.epoch + timedelta(days=1),
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            activate(f, fixture, command, config.epoch), cancel(), return_exceptions=True
        ),
        12,
    )
    assert not isinstance(results[1], Exception), results
    assert not isinstance(results[0], Exception) or isinstance(results[0], RouteAuthorityError), (
        results
    )
    async with f() as session:
        assert (await session.get(CollectionRequest, ids[0])).status == "CANCELLED"
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == ids[0])
        )
        if pickup is not None:
            assert (
                await session.scalar(
                    select(RiderAssignmentItem.assignment_id).where(
                        RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id,
                        RiderAssignmentItem.released_at.is_(None),
                    )
                )
                is None
            )
        assert await session.scalar(select(func.count()).select_from(Refund)) == 1


async def test_failed_pickup_prefix_and_release_cannot_return_to_v1(database_session_factory):
    from tirodhan.modules.dispatch.service import PickupGroupNotAssignableError

    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(f)
    await activate(f, fixture, command, config.epoch)
    root, revision, tasks = await root_revision(f)
    now = config.epoch + timedelta(minutes=30)
    await execute(
        f,
        fixture.rider_ids[0],
        StartTask(
            client_command_id=new_uuid7(),
            task_id=tasks[0].task_id,
            revision_id=revision.revision_id,
        ),
        now,
    )
    attempt = PickupTask(
        client_command_id=new_uuid7(),
        task_id=tasks[0].task_id,
        revision_id=revision.revision_id,
        outcome="NOT_COLLECTED",
    )
    attempt_id = await execute(f, fixture.rider_ids[0], attempt, now)
    assert await execute(f, fixture.rider_ids[0], attempt, now) == attempt_id
    with pytest.raises(RouteAuthorityError, match="TASK_NOT_NEXT"):
        await execute(
            f,
            fixture.rider_ids[0],
            StartTask(
                client_command_id=new_uuid7(),
                task_id=tasks[1].task_id,
                revision_id=revision.revision_id,
            ),
            now,
        )
    await recover_route(
        f,
        manager_id=fixture.manager_id,
        command=RecoveryCommand(
            client_command_id=new_uuid7(),
            route_id=root.route_id,
            revision_id=revision.revision_id,
            reason_code="EXECUTION_EXCEPTION",
        ),
        expires_at=now + timedelta(days=1),
        now=now,
    )
    with pytest.raises(PickupGroupNotAssignableError):
        await assign_group_manually(
            f,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
        )
    async with f() as session:
        assert (await session.get(VehicleRoute, root.route_id)).status == "CLOSED"
        assert (await session.get(PickupAttempt, attempt_id)).outcome == "NOT_COLLECTED"
        assert await session.scalar(select(func.count()).select_from(CargoVehicleObservation)) == 0


@pytest.mark.parametrize("engine", list(OptimizerEngine))
@pytest.mark.routing_engine
async def test_native_authoritative_revision_and_complete_custody(database_session_factory, engine):
    import os

    if os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1":
        pytest.skip("pinned native execution is opt-in")
    from geoalchemy2.elements import WKTElement

    from tirodhan.core.logging import configure_logging
    from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
    from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
    from tirodhan.modules.receiving_points.models import ReceivingPoint
    from tirodhan.modules.routing_operations.commands import (
        PositionReport,
        execute_resource_command,
    )

    f = database_session_factory
    fixture, vehicle, point, ids, config, _, command = await prepared(
        f, advance=True, unplanned=True, position=(77.207, 28.632)
    )
    async with f() as session, session.begin():
        await session.execute(
            update(ReceivingPoint).values(location=WKTElement("POINT(77.211 28.630)", srid=4326))
        )
        for request_id, xy in zip(
            ids, ("POINT(77.208 28.631)", "POINT(77.210 28.631)"), strict=True
        ):
            await session.execute(
                update(CollectionRequest)
                .where(CollectionRequest.request_id == request_id)
                .values(pickup_location=WKTElement(xy, srid=4326))
            )
    configure_logging("WARNING", None)
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0)) as client:
        source = OSRMMatrixProvider(
            client=client,
            endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
            graph_version=os.environ["TIRODHAN_OSRM_GRAPH_VERSION"],
            profile_version="test-car",
            max_nodes=30,
            max_snap_distance_m=500,
        )
        optimizers = {
            OptimizerEngine.VROOM: VroomOptimizer(),
            OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
        }
        now = config.epoch
        run = await process(
            f,
            await requested(f, fixture, config),
            config,
            optimizers=optimizers,
            provider=source,
            clock=lambda: now,
        )
        assert run.status == "SUCCEEDED", run.results
        await activate(
            f,
            fixture,
            ActivationCommand(client_command_id=new_uuid7(), run_id=run.run_id, engine=engine),
            now,
        )
        root, revision, tasks = await root_revision(f)
        async with f() as session:
            scheduled = await session.get(RouteRevisionStop, (revision.revision_id, 0))
        now = scheduled.planned_start
        await execute(
            f,
            fixture.rider_ids[0],
            StartTask(
                client_command_id=new_uuid7(),
                task_id=tasks[0].task_id,
                revision_id=revision.revision_id,
            ),
            now,
        )
        await execute(
            f,
            fixture.rider_ids[0],
            PickupTask(
                client_command_id=new_uuid7(),
                task_id=tasks[0].task_id,
                revision_id=revision.revision_id,
                outcome="COLLECTED",
            ),
            now + timedelta(seconds=30),
        )
        now += timedelta(seconds=30)
        ids.append(await add_booking(f, config, point, ids[0], fixture.manager_id, road=True))
        await execute_resource_command(
            f,
            manager_id=fixture.manager_id,
            command=PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicle,
                longitude=77.208,
                latitude=28.631,
                observed_at=now,
            ),
            expires_at=now + timedelta(days=1),
            now=now,
        )
        suffix = await process(
            f,
            await requested(f, fixture, config),
            config,
            optimizers=optimizers,
            provider=source,
            clock=lambda: now,
        )
        assert suffix.status == "SUCCEEDED", suffix.results
        await activate(
            f,
            fixture,
            ActivationCommand(client_command_id=new_uuid7(), run_id=suffix.run_id, engine=engine),
            now,
        )
        root, revision, remaining = await root_revision(f)
        assert root.version == 2 and len(remaining) == 5
        for task in remaining:
            async with f() as session:
                schedule = await session.scalar(
                    select(RouteRevisionStop).where(
                        RouteRevisionStop.revision_id == revision.revision_id,
                        RouteRevisionStop.task_id == task.task_id,
                    )
                )
            now = max(now, schedule.planned_start)
            await execute(
                f,
                fixture.rider_ids[0],
                StartTask(
                    client_command_id=new_uuid7(),
                    task_id=task.task_id,
                    revision_id=revision.revision_id,
                ),
                now,
            )
            if task.kind == "PICKUP":
                action = PickupTask(
                    client_command_id=new_uuid7(),
                    task_id=task.task_id,
                    revision_id=revision.revision_id,
                    outcome="COLLECTED",
                )
            else:
                action = DischargeTask(
                    client_command_id=new_uuid7(),
                    task_id=task.task_id,
                    revision_id=revision.revision_id,
                    scanned_pickup_id=task.pickup_execution_id,
                    scanned_receiving_identifier="test-kiosk-42",
                    client_capture_id=new_uuid7(),
                    captured_at=now,
                    longitude=77.211,
                    latitude=28.630,
                )
            await execute(f, fixture.rider_ids[0], action, now + timedelta(seconds=30))
            now += timedelta(seconds=30)
        async with f() as session:
            assert (await session.get(VehicleRoute, root.route_id)).status == "CLOSED"
            assert (await session.get(RiderAvailability, fixture.rider_ids[0])).work_state == "IDLE"
            assert await session.scalar(select(func.count()).select_from(RouteDischargeProof)) == 3
            assert await session.scalar(select(func.count()).select_from(PickupAttempt)) == 3
        from tirodhan.modules.collection_requests.service import complete_collection_request
        from tirodhan.modules.evidence.service import record_evidence_capture

        for request_id in ids:
            async with f() as session:
                pickup = await session.scalar(
                    select(PickupExecution).where(PickupExecution.request_id == request_id)
                )
            await record_evidence_capture(
                f,
                client_capture_id=new_uuid7(),
                captured_by_user_id=fixture.rider_ids[0],
                target_kind="PICKUP",
                target_id=pickup.pickup_execution_id,
                captured_at=now,
                idempotency_expires_at=now + timedelta(days=1),
                now=now,
            )
            assert (await complete_collection_request(f, request_id, now=now)).status == "COMPLETED"
        print(
            {
                "engine": engine.value,
                "initial": run.results[engine.value]["travel_seconds"],
                "suffix": suffix.results[engine.value]["travel_seconds"],
                "initial_m": run.results[engine.value]["distance_m"],
                "suffix_m": suffix.results[engine.value]["distance_m"],
            }
        )
