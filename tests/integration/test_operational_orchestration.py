"""Real PostgreSQL invariants for regional admission and operational consequences."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import h3
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_routing_dispatch import execute
from test_routing_fleet import LineMatrix, engines, fixture, policy, tick
from test_routing_operations import _token_for

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAvailability
from tirodhan.modules.pickups.models import PickupAttempt
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.routing_operations.admission import ReserveCapacity, reserve_capacity
from tirodhan.modules.routing_operations.admission_jobs import request_admission, run_admissions
from tirodhan.modules.routing_operations.authority import RouteAuthorityError
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.dispatch_models import RouteTask, VehicleRoute
from tirodhan.modules.routing_operations.execution import (
    DepartTask,
    PickupTask,
    StartTask,
)
from tirodhan.modules.routing_operations.operational_commands import (
    AcknowledgeRoute,
    ConfigureRegion,
    DeclareBreak,
    execute_operational,
)
from tirodhan.modules.routing_operations.orchestration import (
    acknowledge_generation,
    prepare_due,
    wake,
)
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityAdmissionRequest,
    CapacityReservation,
    FleetWakeup,
    OperatingRegion,
    PickupReattempt,
)
from tirodhan.modules.routing_operations.rolling import ConfigureFleet, configure_fleet
from tirodhan.modules.routing_operations.synchronization import customer_routing
from tirodhan.modules.serviceability.models import ServiceabilityContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def operational(factory, actor, command, now):
    return await execute_operational(
        factory, actor_id=actor, command=command, expires_at=now + timedelta(days=1), now=now
    )


@pytest.mark.parametrize("opponent", ["break", "cancel"])
async def test_real_overlapping_start_has_one_valid_winner(database_session_factory, opponent):
    from test_fleet_hypothesis_concurrency import overlap

    from tirodhan.modules.collection_requests.cancellation import (
        cancel_collection_request_by_customer,
    )
    from tirodhan.modules.payments.models import Payment
    from tirodhan.modules.routing_operations.execution import current_tasks
    from tirodhan.modules.routing_operations.models import OperatingVehicle

    factory = database_session_factory
    f, vehicles, _, _, _, _, clock, *_ = await regional(factory)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision = await route_revision(factory, vehicles[0])
    await ack(factory, root, revision, f.rider_ids[0], clock[0])
    async with factory() as session:
        task = (await current_tasks(session, root))[0]
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        booking = await session.get(CollectionRequest, pickup.request_id)
        payment = await session.scalar(
            select(Payment).where(Payment.request_id == booking.request_id)
        )
    at = booking.slot_start

    async def start():
        return await execute(
            factory,
            f.rider_ids[0],
            StartTask(
                client_command_id=new_uuid7(),
                task_id=task.task_id,
                revision_id=revision.revision_id,
            ),
            at,
        )

    async def other():
        if opponent == "break":
            return await operational(
                factory,
                f.rider_ids[0],
                DeclareBreak(
                    client_command_id=new_uuid7(),
                    shift_id=root.shift_id,
                    starts_at=at,
                    ends_at=at + timedelta(seconds=60),
                ),
                at,
            )
        async with factory() as session, session.begin():
            return await cancel_collection_request_by_customer(
                session,
                request_id=booking.request_id,
                customer_id=booking.customer_id,
                idempotency_key=str(new_uuid7()),
                planning_lead_time_minutes=15,
                idempotency_expires_at=clock[0] + timedelta(days=1),
            )

    results = await overlap(
        factory,
        OperatingVehicle if opponent == "break" else Payment,
        vehicles[0] if opponent == "break" else payment.payment_id,
        [start, other],
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    async with factory() as session:
        fresh = await session.get(CollectionRequest, booking.request_id)
        current = await session.get(RouteTask, task.task_id)
        assert not (fresh.status == "CANCELLED" and current.state == "STARTED")


@pytest.mark.parametrize("after_upper", [False, True])
async def test_service_start_upper_boundary_is_inclusive(database_session_factory, after_upper):
    from tirodhan.modules.routing_operations.commands import PositionReport
    from tirodhan.modules.routing_operations.execution import current_tasks

    factory = database_session_factory
    f, vehicles, _, _, _, resource, clock, *_ = await regional(factory)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision = await route_revision(factory, vehicles[0])
    await ack(factory, root, revision, f.rider_ids[0], clock[0])
    async with factory() as session:
        task = (await current_tasks(session, root))[0]
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        booking = await session.get(CollectionRequest, pickup.request_id)
    at = booking.slot_end + (timedelta(microseconds=1) if after_upper else timedelta())
    await resource(
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=vehicles[0],
            longitude=77.207,
            latitude=28.632,
            observed_at=at,
        ),
        at=at,
    )
    command = StartTask(
        client_command_id=new_uuid7(), task_id=task.task_id, revision_id=revision.revision_id
    )
    if after_upper:
        with pytest.raises(RouteAuthorityError, match="PICKUP_WINDOW_OR_BOOKING_INVALID"):
            await execute(factory, f.rider_ids[0], command, at)
    else:
        assert await execute(factory, f.rider_ids[0], command, at) == task.task_id


async def test_new_appointment_requires_customer_consent_and_preserves_financial_terms(
    database_session_factory,
):
    from tirodhan.modules.payments.models import Payment
    from tirodhan.modules.routing_operations.execution import current_tasks
    from tirodhan.modules.routing_operations.operational_commands import ConsentAppointment
    from tirodhan.modules.routing_operations.physical import appointment_window

    factory = database_session_factory
    f, vehicles, _, ids, _, _, clock, _, customer, context = await regional(factory, second=True)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision = await route_revision(factory, vehicles[0])
    await ack(factory, root, revision, f.rider_ids[0], clock[0])
    async with factory() as session:
        task = (await current_tasks(session, root))[0]
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        booking = await session.get(CollectionRequest, pickup.request_id)
        original = booking.slot_start, booking.slot_end, booking.quoted_amount_minor
        context = booking.serviceability_context_id
        customer = booking.customer_id
    await _token_for(factory, customer, roles=("CUSTOMER",))
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
            outcome="NOT_COLLECTED",
            failed_reason="CUSTOMER_UNAVAILABLE",
            client_capture_id=new_uuid7(),
            captured_at=at,
        ),
        at,
    )
    reserve_command = admission(context, booking.slot_end, reattempt_request_id=booking.request_id)
    reserved = await reserve(factory, customer, reserve_command, at)
    async with factory() as session:
        assert await appointment_window(session, booking.request_id, *original[:2]) == original[:2]
    consent = ConsentAppointment(
        client_command_id=new_uuid7(), request_id=booking.request_id, reservation_id=reserved
    )
    await operational(factory, customer, consent, at)
    assert await operational(factory, customer, consent, at)
    async with factory() as session:
        fresh = await session.get(CollectionRequest, booking.request_id)
        assert (fresh.slot_start, fresh.slot_end, fresh.quoted_amount_minor) == original
        assert await appointment_window(session, booking.request_id, *original[:2]) == (
            reserve_command.slot_start,
            reserve_command.slot_end,
        )
        payment = await session.scalar(
            select(Payment).where(Payment.request_id == booking.request_id)
        )
        assert payment.status == "SUCCEEDED" and payment.amount_minor == original[2]


async def test_flexible_capacity_is_zero_until_one_rider_explicitly_accepts(
    database_session_factory,
):
    from test_fleet_hypothesis_concurrency import overlap

    from tirodhan.modules.routing_operations.commands import ShiftClose, ShiftOpen
    from tirodhan.modules.routing_operations.models import OperatingShift
    from tirodhan.modules.routing_operations.operational_commands import AcceptAdmissionOffer
    from tirodhan.modules.routing_operations.orchestration_models import (
        AdmissionFlexibleCommitment,
        AdmissionFlexibleOffer,
    )

    factory = database_session_factory
    f, vehicles, _, _, _, resource, clock, _, customer, context = await regional(
        factory, second=True, empty=True
    )
    for vehicle in vehicles:
        async with factory() as session:
            shift = await session.scalar(
                select(OperatingShift).where(
                    OperatingShift.vehicle_id == vehicle, OperatingShift.closed_at.is_(None)
                )
            )
        await resource(ShiftClose(client_command_id=new_uuid7(), shift_id=shift.shift_id))
        await resource(
            ShiftOpen(
                client_command_id=new_uuid7(),
                rider_id=shift.rider_id,
                vehicle_id=vehicle,
                starts_at=clock[0],
                ends_at=clock[0] + timedelta(hours=4),
                resource_kind="FLEXIBLE",
                commitment_id=new_uuid7(),
            )
        )
    command = admission(context, clock[0])
    await request_admission(
        factory,
        customer_id=customer,
        command=command,
        expires_at=clock[0] + timedelta(hours=1),
        now=clock[0],
    )
    await run_admissions(
        factory, providers={"test-car": LineMatrix()}, optimizers=engines(), clock=lambda: clock[0]
    )
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        assert row.status == "NEEDS_ACCEPTANCE"
        assert await session.get(CapacityReservation, command.client_command_id) is None
        offers = list(
            await session.scalars(
                select(AdmissionFlexibleOffer).order_by(AdmissionFlexibleOffer.shift_id)
            )
        )
        shifts = {s.shift_id: s.rider_id for s in await session.scalars(select(OperatingShift))}
    assert len(offers) == 2
    results = await overlap(
        factory,
        CapacityAdmissionRequest,
        command.client_command_id,
        [
            lambda offer=o: operational(
                factory,
                shifts[offer.shift_id],
                AcceptAdmissionOffer(client_command_id=new_uuid7(), offer_id=offer.offer_id),
                clock[0],
            )
            for o in offers
        ],
    )
    assert sum(isinstance(r, RouteAuthorityError) for r in results) == 1
    await run_admissions(
        factory, providers={"test-car": LineMatrix()}, optimizers=engines(), clock=lambda: clock[0]
    )
    async with factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(AdmissionFlexibleCommitment)) == 1
        )
        assert await session.get(CapacityReservation, command.client_command_id) is not None
        assert await session.scalar(select(func.count()).select_from(VehicleRoute)) == 0


async def test_closed_kiosk_uses_an_authorized_alternative_and_preserves_original_manifest(
    database_session_factory,
):
    from test_handover import create_receiving_point

    from tirodhan.modules.customers.service import GeoPoint
    from tirodhan.modules.receiving_points.models import ReceivingPoint
    from tirodhan.modules.routing_operations.commands import WindowDeclare
    from tirodhan.modules.routing_operations.destinations import recover_destinations
    from tirodhan.modules.routing_operations.models import RoutingManifest
    from tirodhan.modules.routing_operations.orchestration_models import (
        DischargeAuthorization,
        RegionReceivingPoint,
    )

    factory = database_session_factory
    f, _, point, ids, config, resource, clock, region, *_ = await regional(factory)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    other = await create_receiving_point(
        factory, location=GeoPoint(latitude=28.630, longitude=77.212)
    )
    await resource(
        WindowDeclare(
            client_command_id=new_uuid7(),
            receiving_point_id=other.receiving_point_id,
            starts_at=clock[0],
            ends_at=config.horizon_end,
        )
    )
    async with factory() as session, session.begin():
        session.add(
            RegionReceivingPoint(region_id=region, receiving_point_id=other.receiving_point_id)
        )
        old = await session.get(ReceivingPoint, point.receiving_point_id)
        old.status = "INACTIVE"
        alternative = await session.get(ReceivingPoint, other.receiving_point_id)
        alternative.official_identifier = "test-alternate-warehouse"
    changed = 0
    for _ in range(3):
        changed += await recover_destinations(
            factory, providers={"test-car": LineMatrix()}, optimizers=engines(), now=clock[0]
        )
    assert changed == len(ids)
    async with factory() as session:
        assert set(await session.scalars(select(DischargeAuthorization.receiving_point_id))) == {
            other.receiving_point_id
        }
        assert set(await session.scalars(select(RoutingManifest.receiving_point_id))) == {
            point.receiving_point_id
        }
    assert (await tick(factory, clock)).status == "ACTIVATED"


@pytest.mark.parametrize("witness", [False, True])
@pytest.mark.parametrize("late", [False, True])
async def test_physical_transfer_requires_both_receipts_and_never_fakes_handover(
    database_session_factory,
    witness,
    late,
):
    from tirodhan.modules.handovers.models import HandoverEvent
    from tirodhan.modules.routing_operations.execution import current_tasks
    from tirodhan.modules.routing_operations.models import OperatingShift
    from tirodhan.modules.routing_operations.operational_commands import (
        CompleteTransfer,
        PrepareTransfer,
        TransferReceipt,
        WitnessTransferReceipt,
        custodian,
    )
    from tirodhan.modules.routing_operations.physical import cargo_load

    factory = database_session_factory
    f, vehicles, point, _, _, _, clock, *_ = await regional(factory, second=True)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision = await route_revision(factory, vehicles[0])
    await ack(factory, root, revision, f.rider_ids[0], clock[0])
    async with factory() as session:
        task = (await current_tasks(session, root))[0]
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        booking = await session.get(CollectionRequest, pickup.request_id)
        target = await session.scalar(
            select(OperatingShift).where(
                OperatingShift.vehicle_id == vehicles[1], OperatingShift.closed_at.is_(None)
            )
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
            outcome="COLLECTED",
        ),
        at,
    )
    if witness:
        from tirodhan.modules.identity.models import UserRole

        async with factory() as session, session.begin():
            await session.execute(
                update(UserRole).where(UserRole.user_id == f.rider_ids[0]).values(revoked_at=at)
            )
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
    transfer = CompleteTransfer(client_command_id=new_uuid7(), intent_id=intent)
    with pytest.raises(RouteAuthorityError, match="BOTH_PHYSICAL_TRANSFER"):
        await operational(factory, f.manager_id, transfer, at)
    if late:
        at += timedelta(minutes=16)
    if witness:
        await operational(
            factory,
            f.manager_id,
            WitnessTransferReceipt(
                client_command_id=new_uuid7(),
                intent_id=intent,
                scanned_request_id=booking.request_id,
                captured_at=at,
                physical_handoff_witnessed=True,
            ),
            at,
        )
    for rider in (target.rider_id,) if witness else (f.rider_ids[0], target.rider_id):
        await operational(
            factory,
            rider,
            TransferReceipt(
                client_command_id=new_uuid7(),
                intent_id=intent,
                scanned_request_id=booking.request_id,
                captured_at=at,
            ),
            at,
        )
    committed = await operational(factory, f.manager_id, transfer, at)
    assert await operational(factory, f.manager_id, transfer, at) == committed
    async with factory() as session:
        assert await custodian(session, booking.request_id) == (vehicles[1], target.rider_id)
        assert await cargo_load(session, vehicles[0]) == (0, 0)
        assert (await cargo_load(session, vehicles[1]))[0] > 0
        assert await session.scalar(select(func.count()).select_from(HandoverEvent)) == 0
        availability = await session.get(RiderAvailability, target.rider_id)
        assert availability.work_state == "BUSY"

    # The handover domain must attribute an eventual physical delivery to the receiver,
    # while preserving the original collector's successful pickup fact.
    from tirodhan.modules.customers.service import GeoPoint
    from tirodhan.modules.handovers.service import (
        HandoverRiderAttributionError,
        record_handover_in_session,
    )

    async with factory() as session, session.begin():
        with pytest.raises(HandoverRiderAttributionError):
            await record_handover_in_session(
                session,
                client_handover_id=new_uuid7(),
                rider_id=f.rider_ids[0],
                receiving_point_id=point.receiving_point_id,
                pickup_execution_ids=[pickup.pickup_execution_id],
                observed_location=GeoPoint(latitude=28.630, longitude=77.211),
                now=at,
            )
    async with factory() as session, session.begin():
        delivered = await record_handover_in_session(
            session,
            client_handover_id=new_uuid7(),
            rider_id=target.rider_id,
            receiving_point_id=point.receiving_point_id,
            pickup_execution_ids=[pickup.pickup_execution_id],
            observed_location=GeoPoint(latitude=28.630, longitude=77.211),
            now=at,
        )
        assert delivered.status == "VALIDATED"
    async with factory() as session:
        assert await cargo_load(session, vehicles[1]) == (0, 0)


async def test_expired_shift_needs_fresh_rider_consent_and_manager_safety_authorization(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.models import OperatingShift
    from tirodhan.modules.routing_operations.operational_commands import (
        AuthorizeExtension,
        ConsentExtension,
    )
    from tirodhan.modules.routing_operations.regional_projection import effective_shift_end

    factory = database_session_factory
    f, vehicles, _, _, _, _, clock, *_ = await regional(factory)
    async with factory() as session:
        shift = await session.scalar(
            select(OperatingShift).where(OperatingShift.vehicle_id == vehicles[0])
        )
    at = shift.ends_at
    extended = at + timedelta(minutes=30)
    consent = await operational(
        factory,
        f.rider_ids[0],
        ConsentExtension(client_command_id=new_uuid7(), shift_id=shift.shift_id, ends_at=extended),
        at,
    )
    command = AuthorizeExtension(
        client_command_id=new_uuid7(), consent_id=consent, safe_conditions_confirmed=True
    )
    with pytest.raises(OperationalInputError, match="FRESH_RIDER_AVAILABILITY"):
        await operational(factory, f.manager_id, command, at + timedelta(seconds=301))
    await operational(factory, f.manager_id, command, at)
    async with factory() as session:
        assert await effective_shift_end(session, shift.shift_id, shift.ends_at) == extended


async def test_financial_fact_wakeup_is_deduplicated_without_claiming_publication(
    database_session_factory,
):
    from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent
    from tirodhan.modules.reliability.primitives import append_outbox_event

    factory = database_session_factory
    data = await regional(factory)
    now = data[6][0]
    async with factory() as session, session.begin():
        event = await append_outbox_event(
            session,
            event_key="inc7-cancel-test",
            aggregate_type="CollectionRequest",
            aggregate_id=data[3][0],
            event_type="CustomerFinancialStatusChanged",
            payload={"request_id": str(data[3][0]), "change": "COLLECTION_CANCELLED"},
            available_at=now,
        )
        event_id = event.outbox_event_id
    await prepare_due(factory, now)
    async with factory() as session:
        wakeup = await session.get(FleetWakeup, "COMMITTED_FLEET")
        generation = wakeup.generation
    await prepare_due(factory, now)
    async with factory() as session:
        assert (await session.get(FleetWakeup, "COMMITTED_FLEET")).generation == generation
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InboxMessage)
                .where(
                    InboxMessage.message_id == str(event_id),
                    InboxMessage.consumer_name == "routing-financial-wakeup",
                )
            )
            == 1
        )
        assert (await session.get(OutboxEvent, event_id)).published_at is None


async def regional(factory, *, second=False, empty=False):
    f, vehicles, point, ids, config, resource, clock = await fixture(factory, second=second)
    cell = h3.latlng_to_cell(28.632, 77.209, 7)
    async with factory() as session, session.begin():
        await session.execute(update(CollectionRequest).values(cell_id=cell))
        await session.execute(
            update(ServiceabilityContext).values(
                cell_id=cell,
                status="SERVICEABLE",
                location=WKTElement("POINT(77.209 28.632)", srid=4326),
                expires_at=clock[0] + timedelta(days=2),
            )
        )
        booking = await session.get(CollectionRequest, ids[0])
        customer = booking.customer_id
        context_id = booking.serviceability_context_id
        if empty:
            await session.execute(
                update(CollectionRequest).values(status="CANCELLED", cancelled_at=clock[0])
            )
    await _token_for(factory, customer, roles=("CUSTOMER",))
    configured = policy(regional=True, planning_lead_seconds=60)
    region = await execute_operational(
        factory,
        actor_id=f.manager_id,
        command=ConfigureRegion(
            client_command_id=new_uuid7(),
            name="synthetic-regional-pilot",
            cells=(cell,),
            vehicle_ids=tuple(vehicles),
            receiving_point_ids=(point.receiving_point_id,),
            default_warehouse_id=point.receiving_point_id,
            fleet=configured,
            safety_percent=100,
        ),
        expires_at=clock[0] + timedelta(days=1),
        now=clock[0],
    )
    await configure_fleet(
        factory,
        manager_id=f.manager_id,
        command=ConfigureFleet(
            client_command_id=new_uuid7(), expected_version=1, policy=configured
        ),
        expires_at=clock[0] + timedelta(days=1),
        now=clock[0],
    )
    return f, vehicles, point, ids, config, resource, clock, region, customer, context_id


def admission(context, at, **changes):
    start = at.replace(second=0, microsecond=0)
    start += timedelta(minutes=30 - start.minute % 30)
    values = dict(
        client_command_id=new_uuid7(),
        context_id=context,
        slot_start=start,
        slot_end=start + timedelta(minutes=30),
        mass_g=1000,
        volume_ml=1000,
        pickup_seconds=10,
        discharge_seconds=10,
    )
    return ReserveCapacity(**dict(values, **changes))


async def reserve(factory, customer, command, now, *, matrix=None, solvers=None):
    return await reserve_capacity(
        factory,
        customer_id=customer,
        command=command,
        providers={"test-car": matrix or LineMatrix()},
        optimizers=solvers or engines(),
        expires_at=now + timedelta(hours=1),
        now=now,
    )


async def ack(factory, root, revision, rider, now):
    return await execute_operational(
        factory,
        actor_id=rider,
        command=AcknowledgeRoute(client_command_id=new_uuid7(), revision_id=revision.revision_id),
        expires_at=now + timedelta(days=1),
        now=now,
    )


async def route_revision(factory, vehicle):
    from tirodhan.modules.routing_operations.dispatch_models import RouteRevision

    async with factory() as session:
        root = await session.scalar(
            select(VehicleRoute).where(
                VehicleRoute.vehicle_id == vehicle, VehicleRoute.status != "CLOSED"
            )
        )
        revision = await session.scalar(
            select(RouteRevision).where(
                RouteRevision.route_id == root.route_id, RouteRevision.version == root.version
            )
        )
        return root, revision


async def test_regional_reservation_replay_and_no_assignment(database_session_factory):
    factory = database_session_factory
    f, vehicles, point, ids, config, resource, clock, region, customer, context = await regional(
        factory, empty=True
    )
    command = admission(context, clock[0])
    solvers, matrix = engines(), LineMatrix()
    result = await reserve(factory, customer, command, clock[0], matrix=matrix, solvers=solvers)
    count = sum(s.calls for s in solvers.values())
    assert (
        await reserve(
            factory, customer, command, clock[0] + timedelta(days=3), matrix=matrix, solvers=solvers
        )
        == result
    )
    assert sum(s.calls for s in solvers.values()) == count == 2
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1
        assert await session.scalar(select(func.count()).select_from(VehicleRoute)) == 0
        assert (await session.get(OperatingRegion, region)).version == 2
    changed = command.model_copy(update={"mass_g": 2000})
    with pytest.raises(OperationalInputError, match="REPLAY_CONFLICT"):
        await reserve(factory, customer, changed, clock[0])


async def test_two_overlapping_admissions_have_one_scarce_capacity_winner(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, region, customer, context = data[6], data[7], data[8], data[9]
    commands = [
        admission(
            context,
            clock[0],
            mass_g=10000,
            volume_ml=10000,
            pickup_seconds=1801,
            discharge_seconds=10,
        )
        for _ in range(2)
    ]
    async with factory() as barrier, barrier.begin():
        await barrier.scalar(
            select(OperatingRegion).where(OperatingRegion.region_id == region).with_for_update()
        )
        tasks = [asyncio.create_task(reserve(factory, customer, cmd, clock[0])) for cmd in commands]
        observed = False
        for _ in range(400):
            async with factory() as observer:
                waiting = await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND xact_start IS NOT NULL"
                    )
                )
            if waiting >= 2:
                observed = True
                break
            await asyncio.sleep(0.01)
        assert observed, "both admissions must overlap in real PostgreSQL transactions"
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 30)
    assert sum(not isinstance(r, BaseException) for r in results) == 1, results
    for r in results:
        if isinstance(r, DBAPIError):
            assert getattr(r.orig, "sqlstate", None) == "40001"
        elif isinstance(r, BaseException):
            assert isinstance(r, OperationalInputError) and "STALE_RETRY" in str(r)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1
    loser = commands[results.index(next(r for r in results if isinstance(r, BaseException)))]
    with pytest.raises(OperationalInputError, match="NO_FEASIBLE_REGIONAL_CAPACITY"):
        await reserve(factory, customer, loser, clock[0])


async def test_admission_worker_prepares_once_and_terminal_replay(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    now, customer, context = data[6][0], data[8], data[9]
    command = admission(context, now)
    row = await request_admission(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=1), now=now
    )
    assert row.status == "PENDING"
    assert await request_admission(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=2), now=now
    )
    assert (
        await run_admissions(
            factory, providers={"test-car": LineMatrix()}, optimizers=engines(), clock=lambda: now
        )
        == 1
    )
    assert (
        await run_admissions(
            factory, providers={"test-car": LineMatrix()}, optimizers=engines(), clock=lambda: now
        )
        == 0
    )
    async with factory() as session:
        persisted = await session.get(CapacityAdmissionRequest, row.admission_id)
        assert persisted.status == "SUCCEEDED", persisted.failure_code


async def test_wake_generation_does_not_lose_new_event(database_session_factory):
    factory = database_session_factory
    data = await regional(factory)
    now = data[6][0]
    async with factory() as session, session.begin():
        await wake(session, "FIRST", now, urgent=True)
    generation = await prepare_due(factory, now)
    async with factory() as session, session.begin():
        await wake(session, "SECOND", now, urgent=True)
    await acknowledge_generation(factory, generation)
    async with factory() as session:
        row = await session.get(FleetWakeup, "COMMITTED_FLEET")
        assert row.generation > row.acknowledged


async def test_acknowledged_wake_starts_a_new_bounded_coalescing_window(database_session_factory):
    factory = database_session_factory
    data = await regional(factory)
    now = data[6][0]
    async with factory() as session, session.begin():
        await wake(session, "INITIAL", now, urgent=True)
    generation = await prepare_due(factory, now)
    await acknowledge_generation(factory, generation)
    async with factory() as session, session.begin():
        await wake(session, "ADDITION", now + timedelta(seconds=10))
    async with factory() as session:
        row = await session.get(FleetWakeup, "COMMITTED_FLEET")
        first_due = row.due_at
        assert first_due > now + timedelta(seconds=10)
    async with factory() as session, session.begin():
        await wake(session, "SECOND_ADDITION", now + timedelta(seconds=20))
    async with factory() as session:
        row = await session.get(FleetWakeup, "COMMITTED_FLEET")
        assert row.due_at == first_due  # Never postpone an already pending batch.


async def test_revision_receipt_and_failed_attempt_preserve_unaffected_work(
    database_session_factory,
):
    factory = database_session_factory
    f, vehicles, point, ids, config, resource, clock, region, customer, context = await regional(
        factory
    )
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision = await route_revision(factory, vehicles[0])
    async with factory() as session:
        task = await session.scalar(
            select(RouteTask)
            .where(
                RouteTask.route_id == root.route_id,
                RouteTask.kind == "PICKUP",
                RouteTask.state == "PENDING",
            )
            .order_by(RouteTask.task_id)
        )
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        booking = await session.get(CollectionRequest, pickup.request_id)
        clock[0] = booking.slot_start
    command = StartTask(
        client_command_id=new_uuid7(), task_id=task.task_id, revision_id=revision.revision_id
    )
    with pytest.raises(RouteAuthorityError, match="RECEIPT_REQUIRED"):
        await execute(factory, f.rider_ids[0], command, clock[0])
    await ack(factory, root, revision, f.rider_ids[0], clock[0])
    await execute(factory, f.rider_ids[0], command, clock[0])
    failed = PickupTask(
        client_command_id=new_uuid7(),
        task_id=task.task_id,
        revision_id=revision.revision_id,
        outcome="NOT_COLLECTED",
        failed_reason="CUSTOMER_UNAVAILABLE",
        client_capture_id=new_uuid7(),
        captured_at=clock[0],
    )
    result = await execute(factory, f.rider_ids[0], failed, clock[0])
    assert await execute(factory, f.rider_ids[0], failed, clock[0]) == result
    async with factory() as session:
        assert (
            await session.get(PickupExecution, pickup.pickup_execution_id)
        ).status == "PENDING_ASSIGNMENT"
        assert await session.get(PickupReattempt, result)
        assert (await session.get(RouteTask, task.task_id)).state == "CANCELLED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PickupAttempt)
                .where(PickupAttempt.pickup_execution_id == pickup.pickup_execution_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RouteTask)
                .where(RouteTask.route_id == root.route_id, RouteTask.state == "PENDING")
            )
            > 0
        )
    with pytest.raises(RouteAuthorityError):
        await execute(
            factory,
            f.rider_ids[0],
            DepartTask(
                client_command_id=new_uuid7(),
                task_id=task.task_id,
                revision_id=revision.revision_id,
            ),
            clock[0],
        )


async def test_break_boundaries_and_customer_eta_are_revision_scoped(database_session_factory):
    factory = database_session_factory
    data = await regional(factory)
    f, vehicles, clock, customer = data[0], data[1], data[6], data[8]
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision = await route_revision(factory, vehicles[0])
    status = await customer_routing(
        factory, customer_id=customer, request_id=data[3][0], now=clock[0]
    )
    assert status["assigned_rider_id"] == f.rider_ids[0] and status["eta"] is not None
    pause = DeclareBreak(
        client_command_id=new_uuid7(),
        shift_id=root.shift_id,
        starts_at=clock[0],
        ends_at=clock[0] + timedelta(minutes=5),
    )
    await execute_operational(
        factory,
        actor_id=f.rider_ids[0],
        command=pause,
        expires_at=clock[0] + timedelta(days=1),
        now=clock[0],
    )
    await ack(factory, root, revision, f.rider_ids[0], clock[0])
    from tirodhan.modules.routing_operations.execution import current_tasks

    async with factory() as session:
        task = (await current_tasks(session, root))[0]
    depart = DepartTask(
        client_command_id=new_uuid7(), task_id=task.task_id, revision_id=revision.revision_id
    )
    with pytest.raises(RouteAuthorityError, match="ON_BREAK"):
        await execute(factory, f.rider_ids[0], depart, pause.starts_at)
    await execute(factory, f.rider_ids[0], depart, pause.ends_at)
    stale = await customer_routing(
        factory, customer_id=customer, request_id=data[3][0], now=clock[0] + timedelta(minutes=10)
    )
    assert stale["eta"] is None and stale["eta_state"] == "UPDATING"
