from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from alembic import command as migrations
from alembic.config import Config
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_payment_preparation import assert_readiness_schema
from test_routing_dispatch import add_booking, collect_first, execute, prepared, root_revision
from test_routing_operations import _token_for

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem, RiderAvailability
from tirodhan.modules.identity.models import RefreshSession
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.planning.routing.adapters import (
    ORTOOLS_VERSION,
    VROOM_VERSION,
    ORToolsOptimizer,
    VroomOptimizer,
)
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ProposedRoute,
    RoadMatrix,
    RouteProposal,
    StopKind,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.planning.routing.validation import validate_proposal
from tirodhan.modules.receiving_points.models import ReceivingPoint
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.routing_operations.authority import RouteAuthorityError
from tirodhan.modules.routing_operations.commands import (
    PositionReport,
    RestrictFacility,
    ShiftOpen,
    VehicleCreate,
    execute_resource_command,
)
from tirodhan.modules.routing_operations.dispatch_models import (
    RouteTask,
    VehicleRoute,
)
from tirodhan.modules.routing_operations.economics import EconomicPolicy
from tirodhan.modules.routing_operations.execution import (
    DepartTask,
    PickupTask,
    StartTask,
)
from tirodhan.modules.routing_operations.fleet_models import (
    FleetControl,
    FleetCycle,
    FleetException,
)
from tirodhan.modules.routing_operations.models import (
    CargoVehicleObservation,
    RoutingShadowRun,
    VehiclePosition,
)
from tirodhan.modules.routing_operations.rolling import (
    SCOPE,
    ConfigureFleet,
    FleetPolicy,
    configure_fleet,
    run_fleet_cycle,
)
from tirodhan.modules.routing_operations.service import PROBLEM_CODEC, PROPOSAL_CODEC
from tirodhan.modules.routing_operations.telemetry import (
    PositionCommand,
    TelemetryPolicy,
    VehicleIssueCommand,
    report_position,
    report_vehicle_issue,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def policy(**changes):
    # Synthetic engineering calibration; no production economic prices/defaults.
    value = FleetPolicy(
        version="FLEET_TEST_1",
        horizon_seconds=14400,
        minimum_interval_seconds=1,
        maximum_interval_seconds=60,
        displacement_m=200,
        destination_divergence_m=100,
        publication_attempts=3,
        max_requests=12,
        max_vehicles=4,
        max_nodes=40,
        max_profiles=2,
        search_seconds=1,
        lease_seconds=120,
        telemetry=TelemetryPolicy(
            max_age_seconds=3600, max_accuracy_m=30, max_speed_m_per_second=30
        ),
        economics=EconomicPolicy(
            version="SYNTHETIC_SECONDS_1",
            additional_vehicle_seconds=300,
            disruption_seconds=10,
            waiting_weight=0,
            window_risk_buffer_seconds=300,
            window_risk_weight=1,
            max_candidate_evaluations=3000,
        ),
    )
    return value.model_copy(update=changes)


class LineMatrix:
    """Deterministic symmetric 2D fixture, not a claim of road-profile accuracy."""

    calls = 0

    async def acquire(self, nodes):
        self.calls += 1
        values = tuple(
            tuple(
                round((abs(a.longitude - b.longitude) + abs(a.latitude - b.latitude)) * 100000)
                for b in nodes
            )
            for a in nodes
        )
        return RoadMatrix(
            new_uuid7(),
            "synthetic-grid-1",
            "test-car",
            tuple(n.node_id for n in nodes),
            values,
            values,
        )


class SeedEngine:
    def __init__(self, engine):
        self.engine, self.calls = engine, 0

    async def propose(self, problem, *, search_seconds):
        self.calls += 1
        # Feasible incumbent seed with explicit optional backlog. The separate insertion
        # baseline and objective decide all allocations, including across both resources.
        orders = {v.vehicle_id: list(v.protected_prefix) for v in problem.vehicles}
        stops = sorted(
            problem.stops,
            key=lambda s: (
                s.kind == StopKind.DISCHARGE,
                s.service_start_window.start,
                str(s.stop_id),
            ),
        )
        for stop in stops:
            if stop.stop_id in {s for route in orders.values() for s in route}:
                continue
            if stop.required:
                owner = stop.bound_vehicle_id or stop.eligible_vehicle_ids[0]
                orders[owner].append(stop.stop_id)
        served = {s for route in orders.values() for s in route}
        return RouteProposal(
            problem.problem_id,
            problem.snapshot_id,
            problem.contract_version,
            problem.policy_version,
            self.engine,
            VROOM_VERSION if self.engine == OptimizerEngine.VROOM else ORTOOLS_VERSION,
            tuple(ProposedRoute(v, tuple(order)) for v, order in orders.items() if order),
            tuple(s.stop_id for s in problem.stops if s.stop_id not in served),
        )


def engines():
    return {e: SeedEngine(e) for e in OptimizerEngine}


async def fixture(factory, *, second=True, idle_position=(77.220, 28.632)):
    f, vehicle, point, ids, config, _, _ = await prepared(factory, advance=True, unplanned=True)
    now = config.epoch + timedelta(seconds=1)
    async with factory() as session, session.begin():
        await session.execute(
            update(ReceivingPoint).values(location=WKTElement("POINT(77.211 28.630)", srid=4326))
        )
        for request_id, x in zip(ids, (77.208, 77.210), strict=True):
            await session.execute(
                update(CollectionRequest)
                .where(CollectionRequest.request_id == request_id)
                .values(pickup_location=WKTElement(f"POINT({x} 28.631)", srid=4326))
            )

    async def resource(command, at=now):
        return await execute_resource_command(
            factory,
            manager_id=f.manager_id,
            command=command,
            expires_at=at + timedelta(days=1),
            now=at,
        )

    await resource(
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=vehicle,
            longitude=77.207,
            latitude=28.632,
            observed_at=now,
        )
    )
    vehicles = [vehicle]
    if second:
        other = await resource(
            VehicleCreate(
                client_command_id=new_uuid7(),
                mass_capacity_g=10000,
                volume_capacity_ml=10000,
                profile_version="test-car",
                end_receiving_point_id=point.receiving_point_id,
            )
        )
        await resource(
            ShiftOpen(
                client_command_id=new_uuid7(),
                vehicle_id=other,
                rider_id=f.rider_ids[1],
                commitment_id=new_uuid7(),
                resource_kind="COMMITTED",
                starts_at=config.epoch,
                ends_at=config.horizon_end,
            )
        )
        await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=other,
                longitude=idle_position[0],
                latitude=idle_position[1],
                observed_at=now,
            )
        )
        vehicles.append(other)
    command = ConfigureFleet(client_command_id=new_uuid7(), expected_version=0, policy=policy())
    result = await configure_fleet(
        factory,
        manager_id=f.manager_id,
        command=command,
        expires_at=now + timedelta(days=1),
        now=now,
    )
    assert (
        await configure_fleet(
            factory,
            manager_id=f.manager_id,
            command=command,
            expires_at=now + timedelta(days=1),
            now=now,
        )
        == result
    )
    return f, vehicles, point, ids, config, resource, [now]


async def tick(factory, clock, matrix=None, optimizers=None):
    return await run_fleet_cycle(
        factory,
        providers={"test-car": matrix or LineMatrix()},
        optimizers=optimizers or engines(),
        clock=lambda: clock[0],
    )


async def owners(factory):
    async with factory() as session:
        return dict(
            (
                await session.execute(
                    select(PickupExecution.request_id, VehicleRoute.vehicle_id)
                    .join(
                        RiderAssignmentItem,
                        RiderAssignmentItem.pickup_execution_id
                        == PickupExecution.pickup_execution_id,
                    )
                    .join(
                        VehicleRoute,
                        VehicleRoute.assignment_id == RiderAssignmentItem.assignment_id,
                    )
                    .where(RiderAssignmentItem.released_at.is_(None))
                )
            ).all()
        )


async def test_collective_allocation_compares_fleet_cost_and_keeps_future_rights(
    database_session_factory,
):
    factory = database_session_factory
    f, vehicles, _, ids, _, _, clock = await fixture(factory)
    result = await tick(factory, clock)
    assert result.status == "ACTIVATED", result.failure_code
    assert set((await owners(factory)).values()) == {vehicles[0]}
    assert result.measurements["collective_insertion_baseline"]["unserved_lots"] == 0
    assert result.measurements["incumbent"]["unserved_lots"] == 2
    async with factory() as session:
        assert {r.status for r in await session.scalars(select(CollectionRequest))} == {"ACCEPTED"}
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1
        assert await session.scalar(select(func.count()).select_from(PickupExecution)) == len(ids)
        assert (await session.get(RiderAvailability, f.rider_ids[0])).work_state == "BUSY"
        assert (await session.get(RiderAvailability, f.rider_ids[1])).work_state == "IDLE"


@pytest.mark.parametrize("idle_better", [False, True])
async def test_loaded_operating_or_idle_vehicle_wins_on_measured_incremental_cost(
    database_session_factory, idle_better
):
    factory = database_session_factory
    f, vehicles, point, ids, config, resource, clock = await fixture(
        factory, idle_position=(77.240, 28.632)
    )
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, _, _, _, attempt, collected_at = await collect_first(factory, f, config)
    new = await add_booking(factory, config, point, ids[0], f.manager_id)
    clock[0] = collected_at + timedelta(seconds=1)
    # A late booking at the idle vehicle can warrant activating it; a booking next to
    # the operating vehicle should instead join its cargo-bearing suffix.
    new_x = 77.230 if idle_better else 77.2105
    async with factory() as session, session.begin():
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == new)
            .values(pickup_location=WKTElement(f"POINT({new_x} 28.631)", srid=4326))
        )
    await resource(
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=vehicles[0],
            longitude=77.208,
            latitude=28.631,
            observed_at=clock[0],
        ),
        clock[0],
    )
    await resource(
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=vehicles[1],
            longitude=new_x if idle_better else 77.240,
            latitude=28.631,
            observed_at=clock[0],
        ),
        clock[0],
    )
    if idle_better:
        # Explicit test calibration: no additional-vehicle premium, so measured time
        # decides. This is not a Tirodhan business price.
        async with factory() as session, session.begin():
            control = await session.get(FleetControl, SCOPE)
            p = policy(
                economics=policy().economics.model_copy(update={"additional_vehicle_seconds": 0})
            )
            control.configuration = p.model_dump(mode="json")
    result = await tick(factory, clock)
    assert result.status == "ACTIVATED", result.failure_code
    assert (await owners(factory))[new] == vehicles[1 if idle_better else 0]
    async with factory() as session:
        observations = list(await session.scalars(select(CargoVehicleObservation)))
        assert len(observations) == 1 and observations[0].pickup_attempt_id == attempt
        assert observations[0].vehicle_id == root.vehicle_id
        assert (await session.get(VehicleRoute, root.route_id)).assignment_id == root.assignment_id
    print("FLEET_ECONOMICS", "idle" if idle_better else "operating", result.measurements)


async def test_positions_during_solve_rebase_without_repeating_engines(database_session_factory):
    factory = database_session_factory
    f, vehicles, _, _, _, resource, clock = await fixture(factory)
    matrix = LineMatrix()
    calls = engines()
    for engine in calls.values():
        original = engine.propose

        async def moving(problem, *, search_seconds, original=original):
            clock[0] += timedelta(seconds=1)
            await resource(
                PositionReport(
                    client_command_id=new_uuid7(),
                    vehicle_id=vehicles[0],
                    longitude=77.207 + (clock[0].second % 10) * 0.00001,
                    latitude=28.632,
                    observed_at=clock[0],
                ),
                clock[0],
            )
            return await original(problem, search_seconds=search_seconds)

        engine.propose = moving
    result = await tick(factory, clock, matrix, calls)
    assert result.status == "ACTIVATED", result.failure_code
    assert matrix.calls == 2 and all(e.calls == 1 for e in calls.values())
    async with factory() as session:
        run = await session.get(RoutingShadowRun, result.run_id)
        problem = PROBLEM_CODEC.validate_python(run.problem)
        position = await session.scalar(
            select(VehiclePosition)
            .where(VehiclePosition.vehicle_id == vehicles[0])
            .order_by(VehiclePosition.revision.desc())
            .limit(1)
        )
        assert (
            next(v for v in problem.vehicles if v.vehicle_id == vehicles[0]).start_node_id
            == position.position_id
        )
        validate_proposal(
            problem, PROPOSAL_CODEC.validate_python(run.results[result.selected_engine]["proposal"])
        )


async def test_departure_protects_leg_and_old_committed_revision_still_progresses(
    database_session_factory,
):
    factory = database_session_factory
    f, vehicles, point, ids, config, resource, clock = await fixture(factory, second=False)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision, tasks = await root_revision(factory)
    first = tasks[0]
    await execute(
        factory,
        f.rider_ids[0],
        DepartTask(
            client_command_id=new_uuid7(), task_id=first.task_id, revision_id=revision.revision_id
        ),
        clock[0],
    )
    await add_booking(factory, config, point, ids[0], f.manager_id)
    clock[0] += timedelta(seconds=1)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    current, new_revision, remaining = await root_revision(factory)
    assert current.route_id == root.route_id and new_revision.revision_id != revision.revision_id
    assert remaining[0].task_id == first.task_id
    async with factory() as session:
        pickup = await session.get(PickupExecution, first.pickup_execution_id)
        booking = await session.get(CollectionRequest, pickup.request_id)
    clock[0] = booking.slot_start
    await resource(
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=vehicles[0],
            longitude=77.208,
            latitude=28.631,
            observed_at=clock[0],
        ),
        clock[0],
    )
    # A delayed push cannot strand a rider executing the same protected leg.
    await execute(
        factory,
        f.rider_ids[0],
        StartTask(
            client_command_id=new_uuid7(), task_id=first.task_id, revision_id=revision.revision_id
        ),
        clock[0],
    )
    await execute(
        factory,
        f.rider_ids[0],
        PickupTask(
            client_command_id=new_uuid7(),
            task_id=first.task_id,
            revision_id=revision.revision_id,
            outcome="COLLECTED",
        ),
        clock[0] + timedelta(seconds=30),
    )
    async with factory() as session:
        assert (await session.get(RouteTask, first.task_id)).state == "COMPLETED"
        assert await session.scalar(select(func.count()).select_from(CargoVehicleObservation)) == 1
        assert (
            await session.scalar(
                select(OutboxEvent.status)
                .where(OutboxEvent.event_type == "VehicleRouteRevised")
                .limit(1)
            )
            == "PENDING"
        )


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "invalid", "matrix"])
async def test_solver_failures_keep_incumbent_and_backlog_recovers(
    database_session_factory, failure
):
    factory = database_session_factory
    f, _, point, ids, config, _, clock = await fixture(factory, second=False)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision, _ = await root_revision(factory)
    new = await add_booking(factory, config, point, ids[0], f.manager_id)
    clock[0] += timedelta(seconds=1)
    failing = engines()

    async def fail(*args, **kwargs):
        if failure == "timeout":
            raise asyncio.TimeoutError
        raise RoutingAdapterError("SOLVER_UNAVAILABLE")

    for engine in failing.values():
        if failure == "invalid":
            original = engine.propose

            async def invalid(problem, *, search_seconds, original=original):
                proposal = await original(problem, search_seconds=search_seconds)
                return replace(proposal, snapshot_id=new_uuid7())

            engine.propose = invalid
        else:
            engine.propose = fail

    class UnavailableMatrix(LineMatrix):
        async def acquire(self, nodes):
            raise RoutingAdapterError("OSRM_UNAVAILABLE")

    failed = await tick(
        factory,
        clock,
        matrix=UnavailableMatrix() if failure == "matrix" else None,
        optimizers=failing,
    )
    assert failed.status == "FAILED"
    assert failed.failure_code == (
        "OSRM_UNAVAILABLE" if failure == "matrix" else "NO_HEALTHY_ENGINE"
    )
    same, same_revision, _ = await root_revision(factory)
    assert same.route_id == root.route_id and same_revision.revision_id == revision.revision_id
    assert new not in await owners(factory)
    clock[0] += timedelta(seconds=1)
    recovered = await tick(factory, clock)
    assert recovered.status == "ACTIVATED" and new in await owners(factory)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(FleetException)) >= 1


async def test_single_owner_crash_takeover_and_stale_publication(database_session_factory):
    factory = database_session_factory
    _, _, _, _, _, _, clock = await fixture(factory)
    entered, release = asyncio.Event(), asyncio.Event()
    solvers = engines()
    original = solvers[OptimizerEngine.VROOM].propose

    async def blocked(problem, *, search_seconds):
        entered.set()
        await release.wait()
        return await original(problem, search_seconds=search_seconds)

    solvers[OptimizerEngine.VROOM].propose = blocked
    old = asyncio.create_task(tick(factory, clock, optimizers=solvers))
    await entered.wait()
    assert await tick(factory, clock) is None
    old.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old
    clock[0] += timedelta(seconds=121)
    replacement = await tick(factory, clock)
    assert replacement.status == "ACTIVATED", replacement.failure_code
    async with factory() as session:
        statuses = set(await session.scalars(select(FleetCycle.status)))
        assert statuses == {"STALE", "ACTIVATED"}
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1


async def test_rider_telemetry_authorization_freshness_replay_and_no_thrash(
    database_session_factory,
):
    factory = database_session_factory
    f, vehicles, _, _, _, resource, clock = await fixture(factory)
    _, login = await _token_for(factory, f.rider_ids[0], roles=("RIDER",))
    from tirodhan.modules.routing_operations.models import OperatingShift

    async with factory() as session:
        shift = await session.scalar(
            select(OperatingShift).where(OperatingShift.vehicle_id == vehicles[0])
        )
    clock[0] += timedelta(seconds=1)
    command = PositionCommand(
        client_command_id=new_uuid7(),
        shift_id=shift.shift_id,
        longitude=77.20701,
        latitude=28.632,
        accuracy_m=5,
        observed_at=clock[0],
    )
    result = await report_position(
        factory,
        rider_id=f.rider_ids[0],
        session_id=login,
        command=command,
        policy=policy().telemetry,
        expires_at=clock[0] + timedelta(days=1),
        now=clock[0],
    )
    assert (
        await report_position(
            factory,
            rider_id=f.rider_ids[0],
            session_id=login,
            command=command,
            policy=policy().telemetry,
            expires_at=clock[0] + timedelta(days=1),
            now=clock[0],
        )
        == result
    )
    with pytest.raises(RouteAuthorityError, match="POSITION_WRONG_SHIFT"):
        await report_position(
            factory,
            rider_id=f.rider_ids[1],
            session_id=login,
            command=command,
            policy=policy().telemetry,
            expires_at=clock[0] + timedelta(days=1),
            now=clock[0],
        )
    for changes, code in [
        ({"observed_at": clock[0] - timedelta(hours=2)}, "POSITION_QUALITY_INVALID"),
        (
            {"observed_at": clock[0] + timedelta(seconds=1), "longitude": 78.0},
            "POSITION_MOVEMENT_IMPLAUSIBLE",
        ),
        ({"accuracy_m": 100}, "POSITION_QUALITY_INVALID"),
    ]:
        with pytest.raises(RouteAuthorityError, match=code):
            await report_position(
                factory,
                rider_id=f.rider_ids[0],
                session_id=login,
                command=command.model_copy(update={"client_command_id": new_uuid7(), **changes}),
                policy=policy().telemetry,
                expires_at=clock[0] + timedelta(days=1),
                now=clock[0] + timedelta(seconds=1),
            )
    solvers = engines()
    assert (await tick(factory, clock, optimizers=solvers)).status == "ACTIVATED"
    clock[0] += timedelta(seconds=1)
    await tick(
        factory, clock, optimizers=solvers
    )  # Settle the newly authoritative business fingerprint.
    calls = sum(s.calls for s in solvers.values())
    for sample in range(5):
        clock[0] += timedelta(seconds=1)
        await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicles[0],
                longitude=77.20701 + sample * 0.000001,
                latitude=28.632,
                observed_at=clock[0],
            ),
            clock[0],
        )
        assert (await tick(factory, clock, optimizers=solvers)).status == "UNCHANGED"
    assert sum(s.calls for s in solvers.values()) == calls
    async with factory() as session, session.begin():
        (await session.get(RefreshSession, login)).revoked_at = clock[0]
    with pytest.raises(RouteAuthorityError, match="LIVE_SESSION_REQUIRED"):
        await report_position(
            factory,
            rider_id=f.rider_ids[0],
            session_id=login,
            command=command,
            policy=policy().telemetry,
            expires_at=clock[0] + timedelta(days=1),
            now=clock[0],
        )


async def test_breakdown_preserves_cargo_and_facility_restriction_is_explicit(
    database_session_factory,
):
    factory = database_session_factory
    f, _, point, _, config, resource, clock = await fixture(factory, second=False)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, _, _, _, attempt, collected = await collect_first(factory, f, config)
    issue = VehicleIssueCommand(
        client_command_id=new_uuid7(), route_id=root.route_id, reason="BREAKDOWN"
    )
    result = await report_vehicle_issue(
        factory,
        rider_id=f.rider_ids[0],
        command=issue,
        expires_at=collected + timedelta(days=1),
        now=collected,
    )
    assert (
        await report_vehicle_issue(
            factory,
            rider_id=f.rider_ids[0],
            command=issue,
            expires_at=collected + timedelta(days=1),
            now=collected,
        )
        == result
    )
    await resource(
        RestrictFacility(
            client_command_id=new_uuid7(),
            receiving_point_id=point.receiving_point_id,
            starts_at=collected,
            ends_at=config.horizon_end,
            reason="FULL",
        ),
        collected,
    )
    async with factory() as session:
        assert (await session.get(VehicleRoute, root.route_id)).status == "RECOVERY_REQUIRED"
        assert (await session.scalar(select(CargoVehicleObservation))).pickup_attempt_id == attempt
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RiderAssignmentItem)
                .where(RiderAssignmentItem.released_at.is_(None))
            )
            == 2
        )
    clock[0] = collected + timedelta(seconds=1)
    failed = await tick(factory, clock)
    assert failed.status == "FAILED" and failed.failure_code == "NO_COMMITTED_CAPACITY"
    async with factory() as session:
        assert (await session.get(RiderAvailability, f.rider_ids[0])).work_state == "BUSY"


async def test_fleet_schema_roundtrip_and_history_guards(database_session_factory, database_engine):
    await asyncio.to_thread(
        migrations.downgrade, Config("alembic.ini"), "0025_vehicle_route_authority"
    )
    await asyncio.to_thread(migrations.upgrade, Config("alembic.ini"), "head")
    async with database_engine.connect() as connection:
        await connection.run_sync(assert_readiness_schema)
    _, _, _, _, _, _, clock = await fixture(database_session_factory)
    assert (await tick(database_session_factory, clock)).status == "ACTIVATED"
    with pytest.raises(DBAPIError, match="0026 downgrade requires|export adopted business"):
        await asyncio.to_thread(
            migrations.downgrade, Config("alembic.ini"), "0025_vehicle_route_authority"
        )
    for sql in (
        "UPDATE routing_manifest SET mass_g = mass_g + 1",
        "DELETE FROM vehicle_position",
        "UPDATE fleet_cycle SET control_version = control_version + 1",
        "UPDATE fleet_cycle SET failure_code = 'REWRITTEN' WHERE status = 'ACTIVATED'",
        "DELETE FROM fleet_cycle",
    ):
        with pytest.raises(DBAPIError):
            async with database_session_factory() as session, session.begin():
                await session.execute(text(sql))


@pytest.mark.routing_engine
@pytest.mark.parametrize(
    "unavailable_engine", [None, OptimizerEngine.VROOM, OptimizerEngine.OR_TOOLS]
)
async def test_native_collective_fleet_and_moving_cargo(
    database_session_factory, unavailable_engine
):
    if os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1":
        pytest.skip("pinned native execution is opt-in")
    factory = database_session_factory
    f, vehicles, point, ids, config, resource, clock = await fixture(
        factory, idle_position=(77.212, 28.632)
    )
    from tirodhan.core.logging import configure_logging

    configure_logging("INFO", None)
    async with httpx.AsyncClient() as client:
        matrix = OSRMMatrixProvider(
            client=client,
            endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
            graph_version=os.environ["TIRODHAN_OSRM_GRAPH_VERSION"],
            profile_version="test-car",
            max_nodes=40,
            max_snap_distance_m=100,
            timeout_seconds=5,
        )
        solvers = {
            OptimizerEngine.VROOM: VroomOptimizer(),
            OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
        }
        moving_x = [77.207, 77.212]
        for solver in solvers.values():
            original = solver.propose

            async def moving_native(problem, *, search_seconds, original=original):
                task = asyncio.create_task(original(problem, search_seconds=search_seconds))
                for index, vehicle in enumerate(vehicles):
                    clock[0] += timedelta(seconds=1)
                    moving_x[index] += 0.000001
                    await resource(
                        PositionReport(
                            client_command_id=new_uuid7(),
                            vehicle_id=vehicle,
                            longitude=moving_x[index],
                            latitude=28.632,
                            observed_at=clock[0],
                        ),
                        clock[0],
                    )
                return await task

            solver.propose = moving_native
        if unavailable_engine:

            async def unavailable(*args, **kwargs):
                raise RoutingAdapterError("SOLVER_UNAVAILABLE")

            solvers[unavailable_engine].propose = unavailable
        first = await tick(factory, clock, matrix, solvers)
        assert first.status == "ACTIVATED", first.failure_code
        assert len(await owners(factory)) == 2
        root, _, tasks = await root_revision(factory)
        async with factory() as session:
            rider = (await session.get(RiderAssignment, root.assignment_id)).rider_id
            booking = await session.get(
                CollectionRequest,
                (await session.get(PickupExecution, tasks[0].pickup_execution_id)).request_id,
            )
        await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=root.vehicle_id,
                longitude=77.208,
                latitude=28.631,
                observed_at=booking.slot_start,
            ),
            booking.slot_start,
        )
        root, _, _, _, attempt, collected = await collect_first(
            factory, replace(f, rider_ids=(rider,)), config
        )
        await add_booking(factory, config, point, ids[0], f.manager_id, road=True)
        clock[0] = collected + timedelta(seconds=1)
        moving_x[:] = [77.208, 77.212]
        for vehicle, x in zip(vehicles, (77.208, 77.212), strict=True):
            await resource(
                PositionReport(
                    client_command_id=new_uuid7(),
                    vehicle_id=vehicle,
                    longitude=x,
                    latitude=28.631,
                    observed_at=clock[0],
                ),
                clock[0],
            )
        second = await tick(factory, clock, matrix, solvers)
        assert second.status == "ACTIVATED", second.failure_code
        assert len(await owners(factory)) == 3
        async with factory() as session:
            assert (
                await session.scalar(select(CargoVehicleObservation))
            ).pickup_attempt_id == attempt
            assert (
                await session.get(VehicleRoute, root.route_id)
            ).assignment_id == root.assignment_id
        print("NATIVE_FLEET", unavailable_engine, first.measurements, second.measurements)
