"""Physical transfer fencing observed in simultaneously live PostgreSQL transactions."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_fleet_hypothesis_concurrency import overlap
from test_operational_orchestration import ack, operational, regional, route_revision
from test_routing_dispatch import execute
from test_routing_fleet import tick

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.routing_operations.authority import RouteAuthorityError
from tirodhan.modules.routing_operations.dispatch_models import RouteTask, VehicleRoute
from tirodhan.modules.routing_operations.execution import PickupTask, StartTask, current_tasks
from tirodhan.modules.routing_operations.models import OperatingShift, OperatingVehicle
from tirodhan.modules.routing_operations.operational_commands import AbortTransfer, PrepareTransfer
from tirodhan.modules.routing_operations.orchestration_models import CargoTransferIntent
from tirodhan.modules.routing_operations.physical import cargo_load

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def loaded_route(factory, *, outcome="COLLECTED"):
    data = await regional(factory, second=True)
    f, vehicles, _, _, _, _, clock, *_ = data
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision = await route_revision(factory, vehicles[0])
    await ack(factory, root, revision, f.rider_ids[0], clock[0])
    async with factory() as session:
        task = (await current_tasks(session, root))[0]
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        booking = await session.get(CollectionRequest, pickup.request_id)
        target = await session.scalar(
            select(OperatingShift).where(OperatingShift.vehicle_id == vehicles[1])
        )
    at = booking.slot_start
    await execute(
        factory,
        f.rider_ids[0],
        StartTask(
            client_command_id=new_uuid7(), task_id=task.task_id, revision_id=revision.revision_id
        ),
        at,
    )
    await execute(
        factory,
        f.rider_ids[0],
        PickupTask(
            client_command_id=new_uuid7(),
            task_id=task.task_id,
            revision_id=revision.revision_id,
            outcome=outcome,
            failed_reason="CUSTOMER_UNAVAILABLE" if outcome == "NOT_COLLECTED" else None,
            client_capture_id=new_uuid7() if outcome == "NOT_COLLECTED" else None,
            captured_at=at if outcome == "NOT_COLLECTED" else None,
        ),
        at,
    )
    return data, root, revision, booking, target, at


async def test_transfer_and_next_household_service_cannot_both_start(database_session_factory):
    factory = database_session_factory
    data, root, revision, booking, target, at = await loaded_route(factory)
    f, vehicles = data[:2]
    async with factory() as session:
        remaining = await current_tasks(session, root)
    assert remaining[0].kind == "PICKUP"
    prepare = PrepareTransfer(
        client_command_id=new_uuid7(),
        request_id=booking.request_id,
        from_vehicle_id=vehicles[0],
        to_shift_id=target.shift_id,
    )
    start = StartTask(
        client_command_id=new_uuid7(),
        task_id=remaining[0].task_id,
        revision_id=revision.revision_id,
    )
    results = await overlap(
        factory,
        OperatingVehicle,
        vehicles[0],
        [
            lambda: operational(factory, f.manager_id, prepare, at),
            lambda: execute(factory, f.rider_ids[0], start, at),
        ],
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1, results
    assert sum(isinstance(result, RouteAuthorityError) for result in results) == 1
    async with factory() as session:
        pending = await session.scalar(select(func.count()).select_from(CargoTransferIntent))
        active_service = await session.scalar(
            select(func.count())
            .select_from(RouteTask)
            .join(VehicleRoute)
            .where(VehicleRoute.vehicle_id == vehicles[0], RouteTask.state == "STARTED")
        )
        assert not (pending and active_service)
        assert (await cargo_load(session, vehicles[0]))[0] > 0


async def test_expired_transfer_does_not_release_cargo_hold_without_evidenced_abort(
    database_session_factory,
):
    factory = database_session_factory
    data, root, revision, booking, target, at = await loaded_route(factory)
    f, vehicles, _, _, _, resource, *_ = data
    intent = await operational(
        factory,
        f.manager_id,
        PrepareTransfer(
            client_command_id=new_uuid7(),
            request_id=booking.request_id,
            from_vehicle_id=vehicles[0],
            to_shift_id=target.shift_id,
        ),
        at,
    )
    expired = at + timedelta(minutes=16)
    from tirodhan.modules.routing_operations.commands import PositionReport

    await resource(
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=vehicles[0],
            longitude=77.208,
            latitude=28.631,
            observed_at=expired,
        ),
        at=expired,
    )
    async with factory() as session:
        next_task = (await current_tasks(session, root))[0]
    start = StartTask(
        client_command_id=new_uuid7(), task_id=next_task.task_id, revision_id=revision.revision_id
    )
    with pytest.raises(RouteAuthorityError, match="PHYSICAL_TRANSFER_IN_PROGRESS"):
        await execute(factory, f.rider_ids[0], start, expired)
    abort = AbortTransfer(
        client_command_id=new_uuid7(),
        intent_id=intent,
        scanned_request_id=booking.request_id,
        client_capture_id=new_uuid7(),
        captured_at=expired,
        material_with_current_custodian=True,
    )
    assert await operational(factory, f.manager_id, abort, expired) == intent
    assert await operational(factory, f.manager_id, abort, expired) == intent
    async with factory() as session:
        row = await session.get(CargoTransferIntent, intent)
        assert row.resolution == "ABORTED" and row.resolution_evidence_id is not None
        assert (await cargo_load(session, vehicles[0]))[0] > 0
        assert await cargo_load(session, vehicles[1]) == (0, 0)


@pytest.mark.parametrize("attempted", [False, True])
async def test_reattempt_consent_and_cancellation_overlap_preserves_existing_rights(
    database_session_factory,
    attempted,
):
    from test_operational_orchestration import admission, reserve
    from test_routing_operations import _token_for

    from tirodhan.modules.collection_requests.cancellation import (
        CancellationConflictError,
        cancel_collection_request_by_customer,
    )
    from tirodhan.modules.payments.models import Payment
    from tirodhan.modules.routing_operations.operational_commands import ConsentAppointment

    factory = database_session_factory
    if attempted:
        _, _, _, booking, _, at = await loaded_route(factory, outcome="NOT_COLLECTED")
    else:
        data = await regional(factory, second=True)
        async with factory() as session:
            booking = await session.get(CollectionRequest, data[3][0])
        at = data[6][0]
    await _token_for(factory, booking.customer_id, roles=("CUSTOMER",))
    command = admission(
        booking.serviceability_context_id, booking.slot_end, reattempt_request_id=booking.request_id
    )
    reservation = await reserve(factory, booking.customer_id, command, at)
    consent = ConsentAppointment(
        client_command_id=new_uuid7(), request_id=booking.request_id, reservation_id=reservation
    )
    async with factory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.request_id == booking.request_id)
        )

    async def cancel():
        async with factory() as session, session.begin():
            return await cancel_collection_request_by_customer(
                session,
                request_id=booking.request_id,
                customer_id=booking.customer_id,
                idempotency_key=str(new_uuid7()),
                planning_lead_time_minutes=15,
                idempotency_expires_at=at + timedelta(days=1),
            )

    results = await overlap(
        factory,
        Payment,
        payment.payment_id,
        [lambda: operational(factory, booking.customer_id, consent, at), cancel],
    )
    if attempted:
        assert isinstance(results[1], CancellationConflictError), results
        assert not isinstance(results[0], Exception), results
    else:
        assert not isinstance(results[1], Exception), results
    async with factory() as session:
        fresh = await session.get(CollectionRequest, booking.request_id)
        assert fresh.status == ("PLANNED" if attempted else "CANCELLED")
        assert (fresh.slot_start, fresh.slot_end, fresh.quoted_amount_minor) == (
            booking.slot_start,
            booking.slot_end,
            booking.quoted_amount_minor,
        )
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == booking.request_id)
        )
        assert pickup is None or pickup.status != "COLLECTED"
    if not attempted:
        with pytest.raises(RouteAuthorityError):
            await operational(
                factory,
                booking.customer_id,
                consent.model_copy(update={"client_command_id": new_uuid7()}),
                at,
            )


async def test_facility_closure_and_discharge_overlap_never_lose_unhanded_cargo(
    database_session_factory,
):
    from tirodhan.modules.handovers.models import HandoverEvent
    from tirodhan.modules.receiving_points.models import ReceivingPoint
    from tirodhan.modules.routing_operations.commands import PositionReport
    from tirodhan.modules.routing_operations.execution import DischargeTask
    from tirodhan.modules.routing_operations.recovery import RecoveryCommand, recover_route

    factory = database_session_factory
    data, root, revision, _, _, at = await loaded_route(factory)
    f, vehicles, point, _, _, resource, *_ = data
    await recover_route(
        factory,
        manager_id=f.manager_id,
        command=RecoveryCommand(
            client_command_id=new_uuid7(),
            route_id=root.route_id,
            revision_id=revision.revision_id,
            reason_code="EXECUTION_EXCEPTION",
        ),
        expires_at=at + timedelta(days=1),
        now=at,
    )
    at += timedelta(seconds=1)
    await resource(
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=vehicles[0],
            longitude=77.211,
            latitude=28.630,
            observed_at=at,
        ),
        at=at,
    )
    async with factory() as session:
        task = (await current_tasks(session, root))[0]
    assert task.kind == "DISCHARGE"
    await execute(
        factory,
        f.rider_ids[0],
        StartTask(
            client_command_id=new_uuid7(), task_id=task.task_id, revision_id=revision.revision_id
        ),
        at,
    )
    proof = DischargeTask(
        client_command_id=new_uuid7(),
        task_id=task.task_id,
        revision_id=revision.revision_id,
        scanned_pickup_id=task.pickup_execution_id,
        scanned_receiving_identifier="test-kiosk-42",
        client_capture_id=new_uuid7(),
        captured_at=at,
        longitude=77.211,
        latitude=28.630,
    )

    async def close():
        async with factory() as session, session.begin():
            facility = await session.scalar(
                select(ReceivingPoint)
                .where(ReceivingPoint.receiving_point_id == point.receiving_point_id)
                .with_for_update()
            )
            facility.status = "INACTIVE"

    results = await overlap(
        factory,
        ReceivingPoint,
        point.receiving_point_id,
        [close, lambda: execute(factory, f.rider_ids[0], proof, at)],
    )
    assert results[0] is None
    async with factory() as session:
        validated = await session.scalar(
            select(func.count())
            .select_from(HandoverEvent)
            .where(HandoverEvent.status == "VALIDATED")
        )
        assert validated in (0, 1)
        load = await cargo_load(session, vehicles[0])
        assert (load == (0, 0)) == (validated == 1)
        assert (await session.get(PickupExecution, task.pickup_execution_id)).status == "COLLECTED"
