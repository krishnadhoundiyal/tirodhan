"""Real PostgreSQL owner, confirmation, outcome and activation boundaries."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_operational_orchestration import operational, regional
from test_routing_fleet import engines, policy

from tirodhan.db.values import new_uuid7
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.routing_operations.authority import RouteAuthorityError
from tirodhan.modules.routing_operations.dispatch_models import RouteActivation
from tirodhan.modules.routing_operations.models import OperatingShift
from tirodhan.modules.routing_operations.operational_commands import ConfirmShift
from tirodhan.modules.routing_operations.orchestration_models import FleetWakeup
from tirodhan.modules.routing_operations.parallel import WorkerResult
from tirodhan.modules.routing_operations.parallel_models import PlanningOutcome
from tirodhan.modules.routing_operations.regional_planner import (
    RegionalPolicy,
    configure_region,
    regional_wake,
    run_region,
    scope,
)

pytest.importorskip("ortools", reason="regional activation requires the optional routing extra")

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def fake_workers(tasks, count):
    results = []
    for task in tasks:
        proposal = await engines()[task.engine].propose(
            task.problem, search_seconds=task.search_seconds
        )
        from tirodhan.modules.routing_operations.economics import insertion_search

        proposal, _, _ = insertion_search(task.problem, proposal, policy().economics, {})
        results.append(WorkerResult(task_id=task.task_id, proposal=proposal, elapsed_seconds=0))
    return results


async def prepared(factory, confirm=True, second=True, max_admitted=12, configure=True):
    values = await regional(factory, second=second)
    f, vehicles, point, ids, config, resource, clock, region_id, *_ = values
    p = RegionalPolicy(
        fleet=policy(regional=True, planning_lead_seconds=600),
        processes=2,
        max_admitted=max_admitted,
        neighbourhood_size=8,
        capacity_multiplier=2,
        max_discovery=5000,
        max_adjustments=50,
        selection_seconds=2,
        starvation_seconds=600,
    )
    if confirm:
        async with factory() as session:
            shifts = list(await session.scalars(select(OperatingShift)))
        for shift in shifts:
            await operational(
                factory,
                shift.rider_id,
                ConfirmShift(client_command_id=new_uuid7(), shift_id=shift.shift_id),
                shift.starts_at - timedelta(seconds=1),
            )
    if configure:
        await configure_region(
            factory, region_id=region_id, manager_id=f.manager_id, policy=p, now=clock[0]
        )
    return values, p


async def run(factory, values, workers=fake_workers):
    from test_routing_fleet import LineMatrix

    return await run_region(
        factory,
        region_id=values[7],
        providers={"test-car": LineMatrix()},
        clock=lambda: values[6][0],
        workers=workers,
    )


async def test_composite_activation_has_outcomes_and_ack_atomic_with_authority(
    database_session_factory,
):
    factory = database_session_factory
    values, p = await prepared(factory)
    cycle = await run(factory, values)
    assert cycle.status == "ACTIVATED"
    async with factory() as session:
        outcomes = list(await session.scalars(select(PlanningOutcome)))
        wake = await session.get(FleetWakeup, scope(values[7]))
        assert {o.request_id for o in outcomes} == set(values[3])
        assert any(o.outcome == "ASSIGNED" for o in outcomes)
        assert wake.acknowledged == wake.generation
        activation = await session.get(RouteActivation, cycle.activation_id)
        assert activation.engine == "COMPOSITE"
    assert await run(factory, values) is None


async def test_unconfirmed_shift_never_contributes_capacity(database_session_factory):
    values, _ = await prepared(database_session_factory, confirm=False)
    cycle = await run(database_session_factory, values)
    async with database_session_factory() as session:
        outcomes = list(await session.scalars(select(PlanningOutcome)))
        assert len(outcomes) == 2
        assert all(o.outcome == "DEFERRED" for o in outcomes)
    assert cycle.status == "UNCHANGED"


async def test_pre_shift_confirmation_replay_and_rider_ownership(database_session_factory):
    factory = database_session_factory
    values, _ = await prepared(factory, confirm=False)
    async with factory() as session:
        shift = await session.scalar(
            select(OperatingShift).where(OperatingShift.rider_id == values[0].rider_ids[0])
        )
    command = ConfirmShift(client_command_id=new_uuid7(), shift_id=shift.shift_id)
    with pytest.raises(RouteAuthorityError, match="PRE_SHIFT_CONFIRMATION"):
        await operational(factory, shift.rider_id, command, shift.starts_at)
    at = shift.starts_at - timedelta(seconds=1)
    assert await operational(factory, shift.rider_id, command, at) == shift.shift_id
    assert await operational(factory, shift.rider_id, command, values[6][0]) == shift.shift_id
    with pytest.raises(RouteAuthorityError, match="COMMITTED_RIDER_SHIFT"):
        await operational(
            factory,
            values[0].rider_ids[1],
            ConfirmShift(client_command_id=new_uuid7(), shift_id=shift.shift_id),
            at,
        )
    with pytest.raises(IdempotencyKeyConflictError):
        await operational(
            factory, shift.rider_id, command.model_copy(update={"shift_id": new_uuid7()}), at
        )


async def test_single_owner_and_new_generation_survive_older_completion(database_session_factory):
    factory = database_session_factory
    values, _ = await prepared(factory)
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(tasks, count):
        entered.set()
        await release.wait()
        return await fake_workers(tasks, count)

    task = asyncio.create_task(run(factory, values, paused))
    await asyncio.wait_for(entered.wait(), 15)
    assert await run(factory, values) is None
    async with factory() as session, session.begin():
        await regional_wake(session, values[7], values[6][0])
    release.set()
    assert (await task).status == "ACTIVATED"
    async with factory() as session:
        wake = await session.get(FleetWakeup, scope(values[7]))
        assert wake.generation > wake.acknowledged


async def test_worker_crash_does_not_ack_and_next_execution_recovers(database_session_factory):
    factory = database_session_factory
    values, _ = await prepared(factory)

    async def crash(tasks, count):
        raise RuntimeError("simulated worker crash")

    with pytest.raises(RuntimeError):
        await run(factory, values, crash)
    async with factory() as session:
        wake = await session.get(FleetWakeup, scope(values[7]))
        assert wake.acknowledged == 0
        assert await session.scalar(select(func.count()).select_from(PlanningOutcome)) == 0
    values[6][0] += timedelta(seconds=2)
    assert (await run(factory, values)).status == "ACTIVATED"


async def test_position_during_workers_is_rebased_before_authority(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.commands import PositionReport

    factory = database_session_factory
    values, _ = await prepared(factory)

    async def move(tasks, count):
        result = await fake_workers(tasks, count)
        values[6][0] += timedelta(seconds=1)
        await values[5](
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=values[1][0],
                longitude=77.2071,
                latitude=28.632,
                observed_at=values[6][0],
            ),
            at=values[6][0],
        )
        return result

    cycle = await run(factory, values, move)
    assert cycle.status == "ACTIVATED"
    from tirodhan.modules.routing_operations.models import RoutingShadowRun

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 1
        shadow = await session.get(RoutingShadowRun, cycle.run_id)
        assert len(shadow.results["COMPOSITE"]["rebase_history"]) == 1
        assert (await session.get(FleetWakeup, scope(values[7]))).acknowledged > 0


async def test_commit_response_loss_replays_one_activation(database_session_factory, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from tirodhan.modules.planning.routing.contracts import SelectionEngine
    from tirodhan.modules.routing_operations.authority import ActivationCommand, activate_proposal
    from tirodhan.modules.routing_operations.fleet_models import FleetCycle

    factory = database_session_factory
    values, _ = await prepared(factory)
    commit = AsyncSession.commit
    fired = False

    async def lost(session):
        nonlocal fired
        await commit(session)
        if not fired:
            fired = True
            raise RuntimeError("response lost after commit")

    monkeypatch.setattr(AsyncSession, "commit", lost)
    with pytest.raises(RuntimeError, match="response lost"):
        await run(factory, values)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    async with factory() as session:
        cycle = await session.scalar(select(FleetCycle).where(FleetCycle.scope == scope(values[7])))
        assert cycle.status == "ACTIVATED"
    replay = await activate_proposal(
        factory,
        manager_id=values[0].manager_id,
        command=ActivationCommand(
            client_command_id=cycle.cycle_id,
            run_id=cycle.run_id,
            engine=SelectionEngine.COMPOSITE,
            fleet_cycle_id=cycle.cycle_id,
        ),
        expires_at=values[6][0] + timedelta(days=1),
        now=values[6][0],
    )
    assert replay == cycle.activation_id
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 1
        assert await session.scalar(select(func.count()).select_from(PlanningOutcome)) == 2


@pytest.mark.parametrize("failure", ["BREAKDOWN", "INFEASIBLE_INCUMBENT"])
async def test_incident_holds_owned_work_but_healthy_rider_can_plan(
    database_session_factory, failure
):
    from sqlalchemy import update
    from test_routing_dispatch import add_booking

    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem
    from tirodhan.modules.routing_operations.dispatch_models import VehicleRoute
    from tirodhan.modules.routing_operations.models import OperatingVehicle

    factory = database_session_factory
    values, _ = await prepared(factory)
    await run(factory, values)
    async with factory() as session:
        root = await session.scalar(
            select(VehicleRoute)
            .join(RiderAssignment)
            .where(RiderAssignment.rider_id == values[0].rider_ids[0])
        )
        assert root is not None
    extra = await add_booking(factory, values[4], values[2], values[3][0], values[0].manager_id)
    async with factory() as session, session.begin():
        original = await session.get(CollectionRequest, values[3][0])
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == extra)
            .values(cell_id=original.cell_id)
        )
        await session.execute(
            update(OperatingVehicle)
            .where(OperatingVehicle.vehicle_id == root.vehicle_id)
            .values(blocked_reason="BREAKDOWN" if failure == "BREAKDOWN" else None)
        )
    if failure == "INFEASIBLE_INCUMBENT":
        from tirodhan.modules.routing_operations.commands import PositionReport

        values[6][0] += timedelta(seconds=1)
        await values[5](
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=root.vehicle_id,
                longitude=78.5,
                latitude=28.632,
                observed_at=values[6][0],
            ),
            at=values[6][0],
        )
    values[6][0] += timedelta(seconds=2)
    result = await run(factory, values)
    assert result.status == "ACTIVATED"
    async with factory() as session:
        assert (await session.get(VehicleRoute, root.route_id)).status == "RECOVERY_REQUIRED"
        outcome = await session.get(PlanningOutcome, (result.cycle_id, extra))
        assert outcome.outcome == "ASSIGNED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RiderAssignmentItem)
                .where(
                    RiderAssignmentItem.assignment_id == root.assignment_id,
                    RiderAssignmentItem.released_at.is_(None),
                )
            )
            > 0
        )

    # A genuine manager-authorized resume restores the held route through normal authority.
    from tirodhan.modules.routing_operations.dispatch_models import RouteRevision
    from tirodhan.modules.routing_operations.operational_commands import ResumeRoute

    async with factory() as session, session.begin():
        await session.execute(
            update(OperatingVehicle)
            .where(OperatingVehicle.vehicle_id == root.vehicle_id)
            .values(blocked_reason=None)
        )
        held = await session.get(VehicleRoute, root.route_id)
        revision = await session.scalar(
            select(RouteRevision).where(
                RouteRevision.route_id == held.route_id, RouteRevision.version == held.version
            )
        )
    if failure == "INFEASIBLE_INCUMBENT":
        await values[5](
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=root.vehicle_id,
                longitude=77.207,
                latitude=28.632,
                observed_at=values[6][0],
            ),
            at=values[6][0],
        )
    await operational(
        factory,
        values[0].manager_id,
        ResumeRoute(client_command_id=new_uuid7(), revision_id=revision.revision_id),
        values[6][0],
    )
    values[6][0] += timedelta(seconds=1)
    assert (await run(factory, values)).status == "ACTIVATED"
    async with factory() as session:
        assert (await session.get(VehicleRoute, root.route_id)).status == "ACTIVE"


async def test_cancellation_consequences_are_reconciled_by_regional_owner(database_session_factory):
    from tirodhan.modules.collection_requests.cancellation import (
        cancel_collection_request_by_customer,
    )
    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.planning.models import PickupExecution
    from tirodhan.modules.routing_operations.dispatch_models import RouteTask

    factory = database_session_factory
    values, _ = await prepared(factory)
    await run(factory, values)
    async with factory() as session, session.begin():
        request = await session.get(CollectionRequest, values[3][0])
        await cancel_collection_request_by_customer(
            session,
            request_id=request.request_id,
            customer_id=request.customer_id,
            idempotency_key=str(new_uuid7()),
            planning_lead_time_minutes=15,
            idempotency_expires_at=values[6][0] + timedelta(days=1),
        )
    values[6][0] += timedelta(seconds=2)
    await run(factory, values)
    async with factory() as session:
        assert (await session.get(CollectionRequest, values[3][0])).status == "CANCELLED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RouteTask)
                .join(PickupExecution)
                .where(
                    PickupExecution.request_id == values[3][0],
                    RouteTask.state.in_(("PENDING", "STARTED")),
                )
            )
            == 0
        )


async def test_expired_owner_cannot_finish_successor_cycle(database_session_factory):
    from sqlalchemy.exc import DBAPIError

    factory = database_session_factory
    values, p = await prepared(factory)
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(tasks, count):
        entered.set()
        await release.wait()
        return await fake_workers(tasks, count)

    old = asyncio.create_task(run(factory, values, paused))
    await asyncio.wait_for(entered.wait(), 15)
    values[6][0] += timedelta(seconds=p.fleet.lease_seconds + 1)
    successor = await run(factory, values)
    assert successor.status == "ACTIVATED"
    release.set()
    with pytest.raises((RouteAuthorityError, DBAPIError)):
        await old
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 1
        assert await session.scalar(select(func.count()).select_from(PlanningOutcome)) == 2


async def test_distinct_regions_have_independent_owners_and_working_sets(database_session_factory):
    import h3
    from test_operational_orchestration import operational
    from test_routing_operations import _token_for

    from tirodhan.modules.routing_operations.commands import (
        PositionReport,
        ShiftOpen,
        VehicleCreate,
    )
    from tirodhan.modules.routing_operations.operational_commands import ConfigureRegion

    factory = database_session_factory
    values, p = await prepared(factory, second=False)
    rider = values[0].rider_ids[1]
    await _token_for(factory, rider, roles=("RIDER",))
    vehicle = await values[5](
        VehicleCreate(
            client_command_id=new_uuid7(),
            mass_capacity_g=10000,
            volume_capacity_ml=10000,
            profile_version="test-car",
            end_receiving_point_id=values[2].receiving_point_id,
        )
    )
    now = values[6][0]
    shift = await values[5](
        ShiftOpen(
            client_command_id=new_uuid7(),
            rider_id=rider,
            vehicle_id=vehicle,
            resource_kind="COMMITTED",
            commitment_id=new_uuid7(),
            starts_at=now - timedelta(seconds=1),
            ends_at=now + timedelta(hours=4),
        )
    )
    await operational(
        factory,
        rider,
        ConfirmShift(client_command_id=new_uuid7(), shift_id=shift),
        now - timedelta(seconds=2),
    )
    await values[5](
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=vehicle,
            longitude=77.207,
            latitude=28.632,
            observed_at=now,
        )
    )
    region = await operational(
        factory,
        values[0].manager_id,
        ConfigureRegion(
            client_command_id=new_uuid7(),
            name="independent-second-region",
            cells=(h3.latlng_to_cell(28.7, 77.3, 7),),
            vehicle_ids=(vehicle,),
            receiving_point_ids=(values[2].receiving_point_id,),
            default_warehouse_id=values[2].receiving_point_id,
            fleet=p.fleet,
            safety_percent=100,
        ),
        now,
    )
    await configure_region(
        factory, region_id=region, manager_id=values[0].manager_id, policy=p, now=now
    )
    from sqlalchemy import update
    from test_routing_dispatch import add_booking

    from tirodhan.modules.collection_requests.models import CollectionRequest

    extra = await add_booking(factory, values[4], values[2], values[3][0], values[0].manager_id)
    async with factory() as session, session.begin():
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == extra)
            .values(cell_id=h3.latlng_to_cell(28.7, 77.3, 7))
        )
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(tasks, count):
        entered.set()
        await release.wait()
        return await fake_workers(tasks, count)

    first = asyncio.create_task(run(factory, values, paused))
    await asyncio.wait_for(entered.wait(), 15)
    other = list(values)
    other[7] = region
    second = await run(factory, other)
    assert second.status == "ACTIVATED"
    assert {item["request_id"] for item in second.measurements["inputs"]} == {str(extra)}
    assert set(second.measurements["assigned"]) == {str(extra)}
    release.set()
    assert (await first).status == "ACTIVATED"


@pytest.mark.parametrize("changes", [1, 3])
async def test_final_publication_fence_retries_bounded_and_never_acks_stale_work(
    database_session_factory, monkeypatch, changes
):
    from tirodhan.modules.routing_operations import regional_planner
    from tirodhan.modules.routing_operations.commands import PositionReport

    factory = database_session_factory
    values, _ = await prepared(factory)
    original = regional_planner._activate
    attempts = 0

    async def racing(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= changes:
            values[6][0] += timedelta(seconds=1)
            await values[5](
                PositionReport(
                    client_command_id=new_uuid7(),
                    vehicle_id=values[1][0],
                    longitude=77.207 + attempts * 0.00001,
                    latitude=28.632,
                    observed_at=values[6][0],
                ),
                at=values[6][0],
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(regional_planner, "_activate", racing)
    if changes == 1:
        assert (await run(factory, values)).status == "ACTIVATED"
        assert attempts == 2
    else:
        with pytest.raises(RouteAuthorityError, match="SOURCE_CHANGED"):
            await run(factory, values)
        assert attempts == 3
        async with factory() as session:
            assert (await session.get(FleetWakeup, scope(values[7]))).acknowledged == 0
            assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 0
        monkeypatch.setattr(regional_planner, "_activate", original)
        assert (await run(factory, values)).status == "ACTIVATED"


async def test_pickup_completed_during_workers_is_not_reintroduced(database_session_factory):
    from test_routing_dispatch import collect_first, root_revision

    from tirodhan.modules.planning.routing.validation import validate_proposal
    from tirodhan.modules.routing_operations.models import CargoVehicleObservation, RoutingShadowRun
    from tirodhan.modules.routing_operations.service import PROBLEM_CODEC, PROPOSAL_CODEC

    factory = database_session_factory
    values, _ = await prepared(factory, second=False)
    assert (await run(factory, values)).status == "ACTIVATED"
    root, revision, tasks = await root_revision(factory)
    pickup_stop = tasks[0].stop_id
    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.routing_operations.operational_commands import AcknowledgeRoute

    await operational(
        factory,
        values[0].rider_ids[0],
        AcknowledgeRoute(
            client_command_id=new_uuid7(),
            revision_id=revision.revision_id,
        ),
        values[6][0],
    )
    async with factory() as session:
        booking = await session.get(CollectionRequest, values[3][0])
        values[6][0] = booking.slot_start

    async def progress(tasks, count):
        proposals = await fake_workers(tasks, count)
        completed = await collect_first(factory, values[0], values[4])
        values[6][0] = completed[-1]
        from tirodhan.modules.routing_operations.commands import PositionReport

        await values[5](
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=values[1][0],
                longitude=77.208,
                latitude=28.631,
                observed_at=values[6][0],
            ),
            at=values[6][0],
        )
        return proposals

    cycle = await run(factory, values, progress)
    assert cycle.status == "ACTIVATED"
    async with factory() as session:
        shadow = await session.get(RoutingShadowRun, cycle.run_id)
        problem = PROBLEM_CODEC.validate_python(shadow.problem)
        proposal = PROPOSAL_CODEC.validate_python(shadow.results["COMPOSITE"]["proposal"])
        validate_proposal(problem, proposal)
        assert pickup_stop not in {stop for route in proposal.routes for stop in route.stop_ids}
        assert sum(len(v.initial_cargo) for v in problem.vehicles) == 1
        assert await session.scalar(select(func.count()).select_from(CargoVehicleObservation)) == 1


async def test_booking_certificate_uses_regional_owner_without_old_global_writer(
    database_session_factory,
):
    from test_operational_orchestration import admission, reserve

    from tirodhan.modules.routing_operations.fleet_models import FleetControl
    from tirodhan.modules.routing_operations.orchestration_models import CapacityReservation

    factory = database_session_factory
    values, _ = await prepared(factory)
    async with factory() as session, session.begin():
        global_control = await session.get(FleetControl, "COMMITTED_FLEET")
        global_control.configuration = {"regional": False}
    command = admission(values[9], values[6][0])
    reservation = await reserve(factory, values[8], command, values[6][0])
    async with factory() as session:
        row = await session.get(CapacityReservation, reservation)
        assert row.region_id == values[7]


async def test_flexible_capacity_requires_offer_acceptance_then_can_activate(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.commands import ShiftClose, ShiftOpen
    from tirodhan.modules.routing_operations.operational_commands import AcceptFlexibleOffer
    from tirodhan.modules.routing_operations.orchestration_models import FlexibleRouteOffer

    factory = database_session_factory
    values, _ = await prepared(factory, max_admitted=1)
    async with factory() as session:
        shifts = list(await session.scalars(select(OperatingShift)))
    for shift in shifts:
        await values[5](ShiftClose(client_command_id=new_uuid7(), shift_id=shift.shift_id))
        await values[5](
            ShiftOpen(
                client_command_id=new_uuid7(),
                rider_id=shift.rider_id,
                vehicle_id=shift.vehicle_id,
                starts_at=values[6][0],
                ends_at=values[6][0] + timedelta(hours=4),
                resource_kind="FLEXIBLE",
                commitment_id=new_uuid7(),
            )
        )
    first = await run(factory, values)
    assert first.status == "UNCHANGED"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 0
        offer = await session.scalar(
            select(FlexibleRouteOffer).order_by(FlexibleRouteOffer.request_id.desc())
        )
        assert offer is not None
        shift = await session.get(OperatingShift, offer.shift_id)
    await operational(
        factory,
        shift.rider_id,
        AcceptFlexibleOffer(client_command_id=new_uuid7(), offer_id=offer.offer_id),
        values[6][0],
    )
    values[6][0] += timedelta(seconds=1)
    second = await run(factory, values)
    assert second.status == "ACTIVATED"
    assert set(second.measurements["assigned"]) == {str(offer.request_id)}

    from tirodhan.modules.routing_operations.parallel_models import ShiftConfirmation

    async with factory() as session:
        assert (await session.get(OperatingShift, shift.shift_id)).resource_kind == "FLEXIBLE"
        assert await session.get(ShiftConfirmation, shift.shift_id) is None


async def test_concurrent_configuration_is_one_exact_regional_initialization(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.fleet_models import FleetControl

    factory = database_session_factory
    values, p = await prepared(factory, configure=False)
    await asyncio.gather(
        *(
            configure_region(
                factory,
                region_id=values[7],
                manager_id=values[0].manager_id,
                policy=p,
                now=values[6][0],
            )
            for _ in range(2)
        )
    )
    async with factory() as session:
        assert (await session.get(FleetWakeup, scope(values[7]))).generation == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(FleetControl)
                .where(FleetControl.region_id == values[7])
            )
            == 1
        )


async def test_new_optional_arrival_does_not_starve_publication_or_ack_its_generation(
    database_session_factory, monkeypatch
):
    from sqlalchemy import update
    from test_routing_dispatch import add_booking

    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.routing_operations import regional_planner

    factory = database_session_factory
    values, _ = await prepared(factory)
    original = regional_planner._activate
    attempts = 0
    extra = None

    async def arrival(*args, **kwargs):
        nonlocal attempts, extra
        attempts += 1
        extra = await add_booking(factory, values[4], values[2], values[3][0], values[0].manager_id)
        async with factory() as session, session.begin():
            booking = await session.get(CollectionRequest, values[3][0])
            await session.execute(
                update(CollectionRequest)
                .where(CollectionRequest.request_id == extra)
                .values(cell_id=booking.cell_id)
            )
            await regional_wake(session, values[7], values[6][0])
        return await original(*args, **kwargs)

    monkeypatch.setattr(regional_planner, "_activate", arrival)
    cycle = await run(factory, values)
    assert cycle.status == "ACTIVATED" and attempts == 1
    async with factory() as session:
        assert await session.get(PlanningOutcome, (cycle.cycle_id, extra)) is None
        wake = await session.get(FleetWakeup, scope(values[7]))
        assert wake.generation > wake.acknowledged
    monkeypatch.setattr(regional_planner, "_activate", original)
    followup = await run(factory, values)
    assert followup.status == "ACTIVATED"
    async with factory() as session:
        assert (
            await session.get(PlanningOutcome, (followup.cycle_id, extra))
        ).outcome == "ASSIGNED"


@pytest.mark.parametrize("failure", ["INVALID_ROAD_MATRIX", "MATRIX_TIMEOUT"])
async def test_road_failure_leaves_generation_pending_and_retry_recovers(
    database_session_factory, failure
):
    from test_routing_fleet import LineMatrix

    from tirodhan.modules.planning.routing.execution import RoutingAdapterError

    factory = database_session_factory
    values, _ = await prepared(factory)

    class Unavailable(LineMatrix):
        async def acquire(self, nodes):
            raise RoutingAdapterError(failure)

    with pytest.raises(RoutingAdapterError, match=failure):
        await run_region(
            factory,
            region_id=values[7],
            providers={"test-car": Unavailable()},
            clock=lambda: values[6][0],
            workers=fake_workers,
        )
    async with factory() as session:
        assert (await session.get(FleetWakeup, scope(values[7]))).acknowledged == 0
        assert await session.scalar(select(func.count()).select_from(RouteActivation)) == 0
        assert await session.scalar(select(func.count()).select_from(PlanningOutcome)) == 0
    values[6][0] += timedelta(seconds=2)
    assert (await run(factory, values)).status == "ACTIVATED"


async def test_waiting_without_confirmed_capacity_escalates_once_and_stays_durable(
    database_session_factory,
):
    from tirodhan.modules.reliability.models import OutboxEvent
    from tirodhan.modules.routing_operations.fleet_models import FleetException

    factory = database_session_factory
    values, p = await prepared(factory, confirm=False)
    assert (await run(factory, values)).status == "UNCHANGED"
    values[6][0] += timedelta(seconds=p.starvation_seconds + 1)
    aged = await run(factory, values)
    values[6][0] += timedelta(seconds=1)
    await run(factory, values)
    async with factory() as session:
        outcomes = list(
            await session.scalars(
                select(PlanningOutcome).where(PlanningOutcome.cycle_id == aged.cycle_id)
            )
        )
        assert {o.request_id for o in outcomes} == set(values[3])
        assert all(o.outcome == "DEFERRED" and o.reason == "WAIT_AGE_ESCALATED" for o in outcomes)
        assert await session.scalar(
            select(func.count())
            .select_from(FleetException)
            .where(FleetException.reason == "WAIT_AGE_ESCALATED")
        ) == len(values[3])
        assert await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "FleetPlanningAttentionRequired")
        ) == len(values[3])


async def test_ended_shift_cargo_stays_in_recovery_while_extended_rider_plans(
    database_session_factory,
):
    from sqlalchemy import update
    from test_routing_dispatch import add_booking, collect_first, root_revision

    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.planning.models import PickupExecution
    from tirodhan.modules.routing_operations.commands import PositionReport, WindowDeclare
    from tirodhan.modules.routing_operations.dispatch_models import VehicleRoute
    from tirodhan.modules.routing_operations.models import CargoVehicleObservation
    from tirodhan.modules.routing_operations.operational_commands import (
        AcknowledgeRoute,
        AuthorizeExtension,
        ConsentExtension,
    )

    factory = database_session_factory
    values, _ = await prepared(factory)
    async with factory() as session:
        other = await session.scalar(
            select(OperatingShift).where(OperatingShift.vehicle_id == values[1][1])
        )
    ends = values[4].epoch + timedelta(hours=8)
    consent = await operational(
        factory,
        other.rider_id,
        ConsentExtension(client_command_id=new_uuid7(), shift_id=other.shift_id, ends_at=ends),
        values[6][0],
    )
    await operational(
        factory,
        values[0].manager_id,
        AuthorizeExtension(
            client_command_id=new_uuid7(), consent_id=consent, safe_conditions_confirmed=True
        ),
        values[6][0],
    )
    await values[5](
        WindowDeclare(
            client_command_id=new_uuid7(),
            receiving_point_id=values[2].receiving_point_id,
            starts_at=values[4].epoch + timedelta(hours=4),
            ends_at=ends,
        )
    )
    await run(factory, values)
    root, revision, tasks = await root_revision(factory)
    async with factory() as session:
        from tirodhan.modules.dispatch.models import RiderAssignment

        owner = (await session.get(RiderAssignment, root.assignment_id)).rider_id
        assert owner != other.rider_id
    await operational(
        factory,
        owner,
        AcknowledgeRoute(client_command_id=new_uuid7(), revision_id=revision.revision_id),
        values[6][0],
    )
    from dataclasses import replace

    picked = await collect_first(factory, replace(values[0], rider_ids=(owner,)), values[4])
    values[6][0] = values[4].epoch + timedelta(hours=4, seconds=1)
    await values[5](
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=other.vehicle_id,
            longitude=77.207,
            latitude=28.632,
            observed_at=values[6][0],
        ),
        at=values[6][0],
    )
    future_config = values[4].model_copy(update={"epoch": values[6][0], "horizon_end": ends})
    extra = await add_booking(factory, future_config, values[2], values[3][0], values[0].manager_id)
    async with factory() as session, session.begin():
        original = await session.get(CollectionRequest, values[3][0])
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == extra)
            .values(cell_id=original.cell_id)
        )
    cycle = await run(factory, values)
    assert cycle.status == "ACTIVATED"
    async with factory() as session:
        assert (await session.get(VehicleRoute, root.route_id)).status == "RECOVERY_REQUIRED"
        assert (
            await session.get(PickupExecution, tasks[0].pickup_execution_id)
        ).status == "COLLECTED"
        observation = await session.get(CargoVehicleObservation, values[3][0])
        assert (
            observation.vehicle_id == root.vehicle_id and observation.pickup_attempt_id == picked[4]
        )
        assert (await session.get(PlanningOutcome, (cycle.cycle_id, extra))).outcome == "ASSIGNED"
        assert (
            await session.get(PlanningOutcome, (cycle.cycle_id, values[3][0]))
        ).outcome == "RECOVERY"
