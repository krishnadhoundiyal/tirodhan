from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from alembic import command as migrations
from alembic.config import Config
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from test_handover import DELHI, create_receiving_point
from test_operational_api import _headers, _token_codec, _token_for
from test_payment_preparation import assert_readiness_schema
from test_rider_dispatch import create_fixture

from tirodhan.core.config import Settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAvailability
from tirodhan.modules.dispatch.service import assign_group_manually
from tirodhan.modules.handovers.models import HandoverEvent
from tirodhan.modules.handovers.service import record_handover
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.pickups.service import record_pickup_attempt, start_assignment
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
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.routing_operations.commands import (
    CargoObserve,
    ManifestDeclare,
    OperationalInputError,
    PositionReport,
    ShiftClose,
    ShiftOpen,
    VehicleCreate,
    WindowDeclare,
    execute_resource_command,
)
from tirodhan.modules.routing_operations.models import (
    OperatingShift,
    OperatingVehicle,
    RoutingShadowRun,
)
from tirodhan.modules.routing_operations.service import (
    PROBLEM_CODEC,
    PROPOSAL_CODEC,
    process_shadow_run,
    request_shadow_run,
)
from tirodhan.modules.routing_operations.snapshot import (
    ShadowConfiguration,
    build_problem,
    capture_snapshot,
)
from tirodhan.modules.serviceability.models import ServiceabilityContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class Matrix:
    calls = 0

    async def acquire(self, nodes):
        self.calls += 1
        values = tuple(
            tuple(0 if i == j else 20 for j in range(len(nodes))) for i in range(len(nodes))
        )
        return RoadMatrix(
            new_uuid7(), "test-graph", "test-car", tuple(n.node_id for n in nodes), values, values
        )


class FeasibleEngine:
    def __init__(self, engine):
        self.engine = engine
        self.calls = 0

    async def propose(self, problem, *, search_seconds):
        self.calls += 1
        # Independent assertions below check schedules/custody; this is an injected test engine.
        stops = sorted(
            problem.stops,
            key=lambda s: (
                s.kind == StopKind.DISCHARGE,
                s.service_start_window.start,
                str(s.stop_id),
            ),
        )
        return RouteProposal(
            problem.problem_id,
            problem.snapshot_id,
            problem.contract_version,
            problem.policy_version,
            self.engine,
            VROOM_VERSION if self.engine == OptimizerEngine.VROOM else ORTOOLS_VERSION,
            (ProposedRoute(problem.vehicles[0].vehicle_id, tuple(s.stop_id for s in stops)),),
            (),
        )


def engines():
    return {e: FeasibleEngine(e) for e in OptimizerEngine}


async def setup(
    factory,
    *,
    manifests=True,
    resource_kind="COMMITTED",
    position=(77.2090, 28.6139),
    position_age_seconds=0,
):
    fixture = await create_fixture(factory, group_count=2, pickups_per_group=1)
    now = utc_now().replace(microsecond=0)
    await _token_for(factory, fixture.manager_id, roles=("MANAGER",))
    point = await create_receiving_point(factory)

    async def execute(command):
        return await execute_resource_command(
            factory,
            manager_id=fixture.manager_id,
            command=command,
            expires_at=now + timedelta(days=1),
            now=now,
        )

    vehicle_id = await execute(
        VehicleCreate(
            client_command_id=new_uuid7(),
            mass_capacity_g=10000,
            volume_capacity_ml=10000,
            profile_version="test-car",
            end_receiving_point_id=point.receiving_point_id,
        )
    )
    shift_id = await execute(
        ShiftOpen(
            client_command_id=new_uuid7(),
            vehicle_id=vehicle_id,
            rider_id=fixture.rider_ids[0],
            commitment_id=new_uuid7(),
            resource_kind=resource_kind,
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(hours=4),
        )
    )
    await execute(
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=vehicle_id,
            longitude=position[0],
            latitude=position[1],
            observed_at=now - timedelta(seconds=position_age_seconds),
        )
    )
    await execute(
        WindowDeclare(
            client_command_id=new_uuid7(),
            receiving_point_id=point.receiving_point_id,
            starts_at=now,
            ends_at=now + timedelta(hours=4),
        )
    )
    async with factory() as session, session.begin():
        requests = list(
            await session.scalars(select(CollectionRequest).order_by(CollectionRequest.request_id))
        )
        ids = []
        for index, request in enumerate(requests):
            ids.append(request.request_id)
            request.cell_id = f"different-cell-{index}"
            request.slot_start = now + timedelta(minutes=30 + index * 60)
            request.slot_end = request.slot_start + timedelta(minutes=30)
    if manifests:
        for request_id in ids:
            await execute(
                ManifestDeclare(
                    client_command_id=new_uuid7(),
                    request_id=request_id,
                    mass_g=4000,
                    volume_ml=4000,
                    pickup_seconds=30,
                    discharge_seconds=30,
                    discharge_deadline=now + timedelta(hours=3),
                    receiving_point_id=point.receiving_point_id,
                )
            )
    config = ShadowConfiguration(
        epoch=now,
        horizon_end=now + timedelta(hours=4),
        policy_version="SHADOW_TEST_V1",
        max_position_age_seconds=300,
        max_requests=10,
        max_vehicles=5,
        max_nodes=30,
        max_profiles=2,
        search_seconds=1,
        lease_seconds=120,
    )
    return fixture, vehicle_id, shift_id, point, ids, config, execute


async def requested(factory, fixture, config):
    return await request_shadow_run(
        factory, manager_id=fixture.manager_id, client_command_id=new_uuid7(), config=config
    )


async def process(factory, run_id, config, *, optimizers=None, provider=None, clock=None):
    return await process_shadow_run(
        factory,
        run_id=run_id,
        providers={"test-car": provider or Matrix()},
        optimizers=optimizers or engines(),
        clock=clock or (lambda: config.epoch),
    )


async def test_cross_cell_slot_persisted_proposals_do_not_freeze_or_dispatch(
    database_session_factory,
):
    factory = database_session_factory
    fixture, vehicle, _, _, ids, config, _ = await setup(factory)
    # Include a genuine accepted advance booking with no PickupExecution yet.
    async with factory() as session, session.begin():
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == ids[0])
        )
        await session.delete(pickup)
        request = await session.get(CollectionRequest, ids[0])
        request.status, request.planning_batch_id = "ACCEPTED", None
        before_events = await session.scalar(select(func.count()).select_from(OutboxEvent))
    matrix, optimizers = Matrix(), engines()
    run_id = await requested(factory, fixture, config)
    run = await process(factory, run_id, config, provider=matrix, optimizers=optimizers)
    assert run.status == "SUCCEEDED" and set(run.results) == {"VROOM", "OR_TOOLS"}
    problem = PROBLEM_CODEC.validate_python(run.problem)
    assert {lot.collection_request_id for lot in problem.lots} == set(ids)
    assert problem.vehicles[0].vehicle_id == vehicle
    for result in run.results.values():
        assessment = validate_proposal(problem, PROPOSAL_CODEC.validate_python(result["proposal"]))
        assert sum(r.travel_seconds for r in assessment.routes) == result["travel_seconds"]
    replay = await process(factory, run_id, config, provider=matrix, optimizers=optimizers)
    assert replay.results == run.results and matrix.calls == 1
    assert all(engine.calls == 1 for engine in optimizers.values())
    async with factory() as session:
        request = await session.get(CollectionRequest, ids[0])
        assert request.status == "ACCEPTED" and request.planning_batch_id is None
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxEvent)) == before_events


async def test_missing_physical_data_is_a_durable_readiness_failure_and_new_command_recovers(
    database_session_factory,
):
    factory = database_session_factory
    fixture, _, _, point, ids, config, execute = await setup(factory, manifests=False)
    run_id = await requested(factory, fixture, config)
    run = await process(factory, run_id, config)
    assert run.status == "FAILED" and run.failure_code == "MANIFEST_REQUIRED"
    assert set(run.results["readiness"]["request_ids"]) == {str(i) for i in ids}
    for request_id in ids:
        await execute(
            ManifestDeclare(
                client_command_id=new_uuid7(),
                request_id=request_id,
                mass_g=1000,
                volume_ml=1000,
                pickup_seconds=10,
                discharge_seconds=10,
                discharge_deadline=config.horizon_end,
                receiving_point_id=point.receiving_point_id,
            )
        )
    assert (await process(factory, run_id, config)).failure_code == "MANIFEST_REQUIRED"
    assert (
        await process(factory, await requested(factory, fixture, config), config)
    ).status == "SUCCEEDED"


@pytest.mark.parametrize(
    "change,expected",
    [
        ("position", "FRESH_POSITION_REQUIRED"),
        ("role", "RESOURCE_AUTHORIZATION_STALE"),
        ("scope", "REQUEST_SCOPE_LIMIT"),
        ("owner", "INCUMBENT_RESOURCE_OUTSIDE_SCOPE"),
    ],
)
async def test_unsafe_or_incomplete_scope_fails_closed(database_session_factory, change, expected):
    factory = database_session_factory
    fixture, vehicle, _, _, _, config, _ = await setup(
        factory, position_age_seconds=600 if change == "position" else 0
    )
    async with factory() as session, session.begin():
        if change == "role":
            await session.execute(
                update(UserRole)
                .where(UserRole.user_id == fixture.rider_ids[0])
                .values(revoked_at=config.epoch)
            )
    if change == "scope":
        config = config.model_copy(update={"max_requests": 1})
    if change == "owner":
        await assign_group_manually(
            factory,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
        )
    run = await process(factory, await requested(factory, fixture, config), config)
    assert run.status == "FAILED" and run.failure_code == expected


async def test_flexible_capacity_cannot_compete_in_committed_stage(database_session_factory):
    fixture, _, _, _, _, config, _ = await setup(database_session_factory, resource_kind="FLEXIBLE")
    run = await process(
        database_session_factory, await requested(database_session_factory, fixture, config), config
    )
    assert run.status == "FAILED" and run.failure_code == "NO_COMMITTED_CAPACITY"


async def test_collected_cargo_survives_idle_and_handover_releases_only_that_lot(
    database_session_factory,
):
    factory = database_session_factory
    fixture, vehicle, shift, _, ids, config, execute = await setup(factory)
    assignment = await assign_group_manually(
        factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        factory, assignment_id=assignment.assignment_id, rider_id=fixture.rider_ids[0]
    )
    await record_pickup_attempt(
        factory,
        pickup_execution_id=fixture.pickup_ids[0][0],
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome="COLLECTED",
    )
    async with factory() as session:
        availability = await session.get(RiderAvailability, fixture.rider_ids[0])
        assert availability.work_state == "IDLE"
        pickup = await session.get(PickupExecution, fixture.pickup_ids[0][0])
        cargo_request = pickup.request_id
    run = await process(factory, await requested(factory, fixture, config), config)
    assert run.failure_code == "CARGO_VEHICLE_OBSERVATION_REQUIRED"
    await execute(
        CargoObserve(client_command_id=new_uuid7(), request_id=cargo_request, vehicle_id=vehicle)
    )
    with pytest.raises(OperationalInputError, match="SHIFT_HAS_OBLIGATIONS"):
        await execute(ShiftClose(client_command_id=new_uuid7(), shift_id=shift))
    run = await process(factory, await requested(factory, fixture, config), config)
    assert run.status == "SUCCEEDED"
    problem = PROBLEM_CODEC.validate_python(run.problem)
    assert problem.vehicles[0].initial_cargo[0].lot_id == cargo_request
    assert next(lot for lot in problem.lots if lot.lot_id == cargo_request).pickup_stop_id is None
    await record_handover(
        factory,
        rider_id=fixture.rider_ids[0],
        client_handover_id=new_uuid7(),
        receiving_point_id=problem.lots[0].authorized_receiving_point_ids[0],
        pickup_execution_ids=(fixture.pickup_ids[0][0],),
        observed_location=DELHI,
        idempotency_expires_at=config.horizon_end,
    )
    new_run = await process(factory, await requested(factory, fixture, config), config)
    assert new_run.status == "SUCCEEDED"
    assert {lot.lot_id for lot in PROBLEM_CODEC.validate_python(new_run.problem).lots} == set(
        ids
    ) - {cargo_request}
    await execute(ShiftClose(client_command_id=new_uuid7(), shift_id=shift))


async def test_busy_owned_work_is_bound_and_preserved_across_groups(database_session_factory):
    factory = database_session_factory
    fixture, vehicle, _, _, _, config, _ = await setup(factory)
    assignment = await assign_group_manually(
        factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        factory, assignment_id=assignment.assignment_id, rider_id=fixture.rider_ids[0]
    )
    run = await process(factory, await requested(factory, fixture, config), config)
    assert run.status == "SUCCEEDED"
    problem = PROBLEM_CODEC.validate_python(run.problem)
    assert any(s.bound_vehicle_id == vehicle for s in problem.stops)
    assert len(problem.lots) == 2


async def test_resource_replay_conflict_and_concurrent_shift_uniqueness(database_session_factory):
    factory = database_session_factory
    fixture, vehicle, shift, point, _, config, execute = await setup(factory)
    command = VehicleCreate(
        client_command_id=new_uuid7(),
        mass_capacity_g=10000,
        volume_capacity_ml=10000,
        profile_version="test-car",
        end_receiving_point_id=point.receiving_point_id,
    )
    results = await asyncio.gather(execute(command), execute(command))
    assert results[0] == results[1]
    with pytest.raises(IdempotencyKeyConflictError):
        await execute(command.model_copy(update={"mass_capacity_g": 20000}))
    await execute(ShiftClose(client_command_id=new_uuid7(), shift_id=shift))
    commands = [
        ShiftOpen(
            client_command_id=new_uuid7(),
            vehicle_id=v,
            rider_id=fixture.rider_ids[0],
            resource_kind="COMMITTED",
            commitment_id=new_uuid7(),
            starts_at=config.epoch,
            ends_at=config.horizon_end,
        )
        for v in (vehicle, results[0])
    ]
    outcomes = await asyncio.gather(*(execute(c) for c in commands), return_exceptions=True)
    assert sum(isinstance(value, UUID) for value in outcomes) == 1
    assert sum(isinstance(value, IntegrityError) for value in outcomes) == 1


async def test_position_replay_and_out_of_order_reports_are_serialized(database_session_factory):
    fixture, vehicle, _, _, _, config, _ = await setup(database_session_factory)
    command = PositionReport(
        client_command_id=new_uuid7(),
        vehicle_id=vehicle,
        longitude=77.2,
        latitude=28.6,
        observed_at=config.epoch + timedelta(seconds=1),
    )

    async def report(c):
        return await execute_resource_command(
            database_session_factory,
            manager_id=fixture.manager_id,
            command=c,
            expires_at=config.horizon_end,
            now=config.epoch + timedelta(seconds=1),
        )

    results = await asyncio.gather(report(command), report(command))
    assert results[0] == results[1]
    with pytest.raises(OperationalInputError, match="POSITION_NOT_MONOTONIC"):
        await report(
            command.model_copy(
                update={"client_command_id": new_uuid7(), "observed_at": config.epoch}
            )
        )


async def test_run_command_uniqueness_and_fingerprint(database_session_factory):
    factory = database_session_factory
    fixture, _, _, _, _, config, _ = await setup(factory)
    client_id = new_uuid7()

    async def request(c):
        return await request_shadow_run(
            factory, manager_id=fixture.manager_id, client_command_id=client_id, config=c
        )

    results = await asyncio.gather(request(config), request(config))
    assert results[0] == results[1]
    with pytest.raises(OperationalInputError, match="RUN_COMMAND_CONFLICT"):
        await request(config.model_copy(update={"policy_version": "changed"}))


async def test_expired_lease_resumes_saved_snapshot_and_old_owner_cannot_publish(
    database_session_factory,
):
    factory = database_session_factory
    fixture, _, _, _, _, config, _ = await setup(factory)
    entered, release = asyncio.Event(), asyncio.Event()

    class Paused(FeasibleEngine):
        async def propose(self, problem, *, search_seconds):
            entered.set()
            await release.wait()
            return await super().propose(problem, search_seconds=search_seconds)

    run_id = await requested(factory, fixture, config)
    first = asyncio.create_task(
        process(
            factory,
            run_id,
            config,
            optimizers={
                OptimizerEngine.VROOM: Paused(OptimizerEngine.VROOM),
                OptimizerEngine.OR_TOOLS: FeasibleEngine(OptimizerEngine.OR_TOOLS),
            },
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(OperationalInputError, match="RUN_ALREADY_LEASED"):
        await process(factory, run_id, config)
    async with factory() as session, session.begin():
        await session.execute(
            update(RoutingShadowRun)
            .where(RoutingShadowRun.run_id == run_id)
            .values(lease_until=config.epoch - timedelta(seconds=1))
        )
    matrix = Matrix()
    second = await process(factory, run_id, config, provider=matrix)
    assert second.status == "SUCCEEDED" and second.attempt == 2 and matrix.calls == 0
    release.set()
    with pytest.raises(OperationalInputError, match="RUN_LEASE_LOST"):
        await first


@pytest.mark.parametrize(
    "change", ["position", "cancellation", "new_booking", "handover_window", "authority"]
)
async def test_source_changes_during_solve_are_not_publishable_as_current(
    database_session_factory, change
):
    factory = database_session_factory
    fixture, vehicle, _, point, ids, config, execute = await setup(factory)

    class Changing(FeasibleEngine):
        async def propose(self, problem, *, search_seconds):
            if change == "position":
                await execute_resource_command(
                    factory,
                    manager_id=fixture.manager_id,
                    command=PositionReport(
                        client_command_id=new_uuid7(),
                        vehicle_id=vehicle,
                        longitude=77.21,
                        latitude=28.62,
                        observed_at=config.epoch + timedelta(seconds=1),
                    ),
                    expires_at=config.horizon_end,
                    now=config.epoch + timedelta(seconds=1),
                )
            elif change == "handover_window":
                await execute(
                    WindowDeclare(
                        client_command_id=new_uuid7(),
                        receiving_point_id=point.receiving_point_id,
                        starts_at=config.epoch + timedelta(minutes=1),
                        ends_at=config.horizon_end,
                    )
                )
            else:
                async with factory() as session, session.begin():
                    if change == "authority":
                        await session.execute(
                            update(AppUser)
                            .where(AppUser.user_id == fixture.rider_ids[0])
                            .values(status="DISABLED")
                        )
                    elif change == "cancellation":
                        await session.execute(
                            update(CollectionRequest)
                            .where(CollectionRequest.request_id == ids[0])
                            .values(status="CANCELLED")
                        )
                    else:
                        source = await session.get(CollectionRequest, ids[0])
                        context = await session.get(
                            ServiceabilityContext, source.serviceability_context_id
                        )
                        context_values = {
                            c.name: getattr(context, c.name)
                            for c in ServiceabilityContext.__table__.columns
                            if c.name != "serviceability_context_id"
                        }
                        created_context = ServiceabilityContext(**context_values)
                        session.add(created_context)
                        await session.flush()
                        values = {
                            c.name: getattr(source, c.name)
                            for c in CollectionRequest.__table__.columns
                            if c.name
                            not in (
                                "request_id",
                                "client_request_id",
                                "serviceability_context_id",
                                "planning_batch_id",
                                "status",
                            )
                        }
                        session.add(
                            CollectionRequest(
                                **values,
                                client_request_id=new_uuid7(),
                                serviceability_context_id=created_context.serviceability_context_id,
                                status="ACCEPTED",
                            )
                        )
            return await super().propose(problem, search_seconds=search_seconds)

    run = await process(
        factory,
        await requested(factory, fixture, config),
        config,
        optimizers={
            OptimizerEngine.VROOM: Changing(OptimizerEngine.VROOM),
            OptimizerEngine.OR_TOOLS: FeasibleEngine(OptimizerEngine.OR_TOOLS),
        },
    )
    assert run.status == "STALE" and run.failure_code == "SOURCE_CHANGED"


async def test_bad_engine_cannot_bypass_validator_or_prevent_other_engine_evaluation(
    database_session_factory,
):
    fixture, _, _, _, _, config, _ = await setup(database_session_factory)

    class Invalid(FeasibleEngine):
        async def propose(self, problem, *, search_seconds):
            result = await super().propose(problem, search_seconds=search_seconds)
            return replace(
                result, routes=(), unserved_stop_ids=tuple(s.stop_id for s in problem.stops)
            )

    optimizers = engines()
    optimizers[OptimizerEngine.VROOM] = Invalid(OptimizerEngine.VROOM)
    run = await process(
        database_session_factory,
        await requested(database_session_factory, fixture, config),
        config,
        optimizers=optimizers,
    )
    assert (
        run.status == "FAILED" and run.results["VROOM"]["failure_code"] == "REQUIRED_STOP_UNSERVED"
    )
    assert (
        "assessment" in run.results["OR_TOOLS"] and optimizers[OptimizerEngine.OR_TOOLS].calls == 1
    )


async def test_matrix_failure_is_replayable_and_new_run_recovers(database_session_factory):
    fixture, _, _, _, _, config, _ = await setup(database_session_factory)

    class FailedMatrix(Matrix):
        async def acquire(self, nodes):
            raise RoutingAdapterError("MATRIX_UNAVAILABLE")

    run_id = await requested(database_session_factory, fixture, config)
    run = await process(database_session_factory, run_id, config, provider=FailedMatrix())
    assert run.status == "FAILED" and run.failure_code == "MATRIX_UNAVAILABLE"
    assert (
        await process(database_session_factory, run_id, config)
    ).failure_code == "MATRIX_UNAVAILABLE"
    assert (
        await process(
            database_session_factory,
            await requested(database_session_factory, fixture, config),
            config,
        )
    ).status == "SUCCEEDED"


async def test_review_api_is_opt_in_and_requires_live_manager(
    database_session_factory, migrated_database_url
):
    fixture, _, _, _, _, config, _ = await setup(database_session_factory)
    disabled = create_app(Settings(_env_file=None, environment="test"))
    assert not any(getattr(route, "path", "").startswith("/v2/") for route in disabled.routes)
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            routing_shadow_review_enabled=True,
            command_idempotency_ttl_seconds=3600,
            database_url=migrated_database_url,
        ),
        access_token_codec=_token_codec(),
    )
    app.state.database_session_factory = database_session_factory
    manager_token, _ = await _token_for(
        database_session_factory, fixture.manager_id, roles=("MANAGER",)
    )
    rider_token, _ = await _token_for(database_session_factory, fixture.rider_ids[0])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://api.test"
    ) as client:
        path = f"/v2/manager/routing/shadow-runs/{new_uuid7()}"
        assert (await client.post(path, json=config.model_dump(mode="json"))).status_code == 401
        assert (
            await client.post(
                path, json=config.model_dump(mode="json"), headers=_headers(rider_token)
            )
        ).status_code == 403
        response = await client.post(
            path, json=config.model_dump(mode="json"), headers=_headers(manager_token)
        )
        assert response.status_code == 200
        result = await client.get(
            f"/v2/manager/routing/shadow-runs/{response.json()['run_id']}",
            headers=_headers(manager_token),
        )
        assert result.json()["authoritative"] is False and "problem" not in result.json()
        resource_path = "/v2/manager/routing/resources"
        payload = PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=(await capture_snapshot(database_session_factory, config)).sources[
                "vehicles"
            ][0]["vehicle_id"],
            longitude=77.22,
            latitude=28.63,
            observed_at=config.epoch,
        ).model_dump(mode="json")
        # A different command with the same observation time is a controlled conflict.
        assert (
            await client.post(resource_path, json=payload, headers=_headers(manager_token))
        ).status_code == 409
        vehicle = (await capture_snapshot(database_session_factory, config)).sources["vehicles"][0]
        create = VehicleCreate(
            client_command_id=new_uuid7(),
            mass_capacity_g=1000,
            volume_capacity_ml=1000,
            profile_version="test-car",
            end_receiving_point_id=vehicle["end_receiving_point_id"],
        ).model_dump(mode="json")
        accepted = await client.post(resource_path, json=create, headers=_headers(manager_token))
        assert accepted.status_code == 200, accepted.text
        assert (
            await client.post(resource_path, json=create, headers=_headers(manager_token))
        ).json() == accepted.json()
        assert (
            await client.post(
                resource_path, json={**create, "unexpected": True}, headers=_headers(manager_token)
            )
        ).status_code == 422


async def test_migration_roundtrip_preserves_existing_history_and_no_new_drift(
    database_engine, database_session_factory
):
    from legacy_financial_fixture import legacy_financial_fixture

    await legacy_financial_fixture(database_session_factory)
    async with database_session_factory() as session:
        before = list(
            await session.scalars(
                text("SELECT to_jsonb(t) FROM collection_request t ORDER BY request_id")
            )
        )
    # Downgrade is destructive for new inputs, not historical booking/execution/financial facts.
    await asyncio.to_thread(
        migrations.downgrade, Config("alembic.ini"), "0023_payment_checkout_readiness"
    )
    await asyncio.to_thread(migrations.upgrade, Config("alembic.ini"), "head")
    async with database_session_factory() as session:
        assert (
            list(
                await session.scalars(
                    text("SELECT to_jsonb(t) FROM collection_request t ORDER BY request_id")
                )
            )
            == before
        )
        assert await session.scalar(select(func.count()).select_from(OperatingVehicle)) == 0
    async with database_engine.connect() as connection:
        await connection.run_sync(assert_readiness_schema)


@pytest.mark.routing_engine
@pytest.mark.parametrize("scenario", ["new", "busy", "cargo"])
async def test_real_road_and_both_engines_from_durable_operational_facts(
    database_session_factory, scenario
):
    if os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1":
        pytest.skip("pinned native execution is opt-in")
    factory = database_session_factory
    fixture, vehicle, _, point, ids, config, execute = await setup(
        factory, position=(77.207, 28.632)
    )
    # Public Delhi road nodes, synthetic household/capacity facts, disposable database only.
    async with factory() as session, session.begin():
        await session.execute(
            update(ReceivingPoint)
            .where(ReceivingPoint.receiving_point_id == point.receiving_point_id)
            .values(location=WKTElement("POINT(77.211 28.630)", srid=4326))
        )
        for request_id, location in zip(
            ids, ("POINT(77.208 28.631)", "POINT(77.210 28.631)"), strict=True
        ):
            await session.execute(
                update(CollectionRequest)
                .where(CollectionRequest.request_id == request_id)
                .values(pickup_location=WKTElement(location, srid=4326))
            )
    if scenario != "new":
        assignment = await assign_group_manually(
            factory,
            collection_group_id=fixture.group_ids[0],
            rider_id=fixture.rider_ids[0],
            manager_user_id=fixture.manager_id,
        )
        await start_assignment(
            factory, assignment_id=assignment.assignment_id, rider_id=fixture.rider_ids[0]
        )
        if scenario == "cargo":
            async with factory() as session, session.begin():
                await session.execute(
                    update(OperatingVehicle)
                    .where(OperatingVehicle.vehicle_id == vehicle)
                    .values(mass_capacity_g=6000, volume_capacity_ml=6000)
                )
            await record_pickup_attempt(
                factory,
                pickup_execution_id=fixture.pickup_ids[0][0],
                rider_id=fixture.rider_ids[0],
                client_attempt_id=new_uuid7(),
                outcome="COLLECTED",
            )
            async with factory() as session:
                pickup = await session.get(PickupExecution, fixture.pickup_ids[0][0])
            await execute(
                CargoObserve(
                    client_command_id=new_uuid7(), request_id=pickup.request_id, vehicle_id=vehicle
                )
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
        run = await process(
            factory,
            await requested(factory, fixture, config),
            config,
            provider=source,
            optimizers={
                OptimizerEngine.VROOM: VroomOptimizer(),
                OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
            },
        )
    assert run.status == "SUCCEEDED", (run.failure_code, run.results)
    problem = PROBLEM_CODEC.validate_python(run.problem)
    assert {lot.collection_request_id for lot in problem.lots} == set(ids)
    for result in run.results.values():
        proposal = PROPOSAL_CODEC.validate_python(result["proposal"])
        validate_proposal(problem, proposal)
        if scenario == "cargo":
            first = next(s for s in problem.stops if s.stop_id == proposal.routes[0].stop_ids[0])
            assert first.kind == StopKind.DISCHARGE
    print(
        {
            "scenario": scenario,
            "results": {
                name: {
                    key: value
                    for key, value in result.items()
                    if key in ("travel_seconds", "distance_m")
                }
                for name, result in run.results.items()
            },
        }
    )


async def test_snapshot_is_consistent_when_another_session_changes_multiple_sources(
    database_session_factory, monkeypatch
):
    from tirodhan.modules.routing_operations import snapshot as module

    factory = database_session_factory
    _, vehicle, _, _, ids, config, _ = await setup(factory)
    original = module._read_sources
    async with factory() as session:
        previous_version = await session.scalar(
            select(OperatingVehicle.version).where(OperatingVehicle.vehicle_id == vehicle)
        )

    async def interleaved(session, configuration):
        await session.execute(select(OperatingVehicle.version))  # Establish the MVCC snapshot.
        async with factory() as writer, writer.begin():
            await writer.execute(
                update(OperatingVehicle)
                .where(OperatingVehicle.vehicle_id == vehicle)
                .values(version=OperatingVehicle.version + 1)
            )
            await writer.execute(
                update(CollectionRequest)
                .where(CollectionRequest.request_id == ids[0])
                .values(status="CANCELLED")
            )
        return await original(session, configuration)

    monkeypatch.setattr(module, "_read_sources", interleaved)
    captured = await capture_snapshot(factory, config)
    assert captured.sources["vehicles"][0]["version"] == previous_version
    assert {r["request_id"] for r in captured.sources["requests"]} == set(ids)
    monkeypatch.setattr(module, "_read_sources", original)
    current = await capture_snapshot(factory, config)
    assert current.fingerprint != captured.fingerprint
    assert {r["request_id"] for r in current.sources["requests"]} == set(ids) - {ids[0]}


async def test_cancelled_worker_can_resume_after_lease_expiry_without_matrix_reacquisition(
    database_session_factory,
):
    factory = database_session_factory
    fixture, _, _, _, _, config, _ = await setup(factory)
    entered = asyncio.Event()

    class Interrupted(FeasibleEngine):
        async def propose(self, problem, *, search_seconds):
            entered.set()
            await asyncio.Event().wait()

    run_id = await requested(factory, fixture, config)
    task = asyncio.create_task(
        process(
            factory,
            run_id,
            config,
            optimizers={
                OptimizerEngine.VROOM: Interrupted(OptimizerEngine.VROOM),
                OptimizerEngine.OR_TOOLS: FeasibleEngine(OptimizerEngine.OR_TOOLS),
            },
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with factory() as session:
        run = await session.get(RoutingShadowRun, run_id)
        assert run.status == "RUNNING" and run.problem is not None
    matrix = Matrix()
    resumed = await process(
        factory,
        run_id,
        config,
        provider=matrix,
        clock=lambda: config.epoch + timedelta(seconds=config.lease_seconds + 1),
    )
    assert resumed.status == "SUCCEEDED" and resumed.attempt == 2 and matrix.calls == 0


async def test_fractional_windows_are_rounded_conservatively(database_session_factory):
    factory = database_session_factory
    _, _, _, _, ids, config, _ = await setup(factory)
    async with factory() as session, session.begin():
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == ids[0])
            .values(
                slot_start=config.epoch + timedelta(seconds=100.1),
                slot_end=config.epoch + timedelta(seconds=200.9),
            )
        )
    problem = await build_problem(
        await capture_snapshot(factory, config),
        config,
        new_uuid7(),
        {"test-car": Matrix()},
        now=config.epoch + timedelta(microseconds=100),
    )
    assert problem.vehicles[0].available_at == 1
    pickup = next(s for s in problem.stops if s.lot_id == ids[0] and s.kind == StopKind.PICKUP)
    assert pickup.service_start_window.start == 101 and pickup.service_start_window.end == 200


async def test_scoped_vehicle_cannot_hide_cargo_owned_by_a_prior_rider(database_session_factory):
    factory = database_session_factory
    fixture, vehicle, shift, _, _, config, execute = await setup(factory)
    assignment = await assign_group_manually(
        factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        factory, assignment_id=assignment.assignment_id, rider_id=fixture.rider_ids[0]
    )
    await record_pickup_attempt(
        factory,
        pickup_execution_id=fixture.pickup_ids[0][0],
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome="COLLECTED",
    )
    async with factory() as session:
        pickup = await session.get(PickupExecution, fixture.pickup_ids[0][0])
    await execute(
        CargoObserve(
            client_command_id=new_uuid7(), request_id=pickup.request_id, vehicle_id=vehicle
        )
    )
    # Model a legacy transition that bypassed coordinated V2 locks. Preserve all custody facts.
    async with factory() as session, session.begin():
        await session.execute(
            update(OperatingShift)
            .where(OperatingShift.shift_id == shift)
            .values(closed_at=config.epoch)
        )
    await execute(
        ShiftOpen(
            client_command_id=new_uuid7(),
            vehicle_id=vehicle,
            rider_id=fixture.rider_ids[1],
            resource_kind="COMMITTED",
            commitment_id=new_uuid7(),
            starts_at=config.epoch,
            ends_at=config.horizon_end,
        )
    )
    run = await process(factory, await requested(factory, fixture, config), config)
    assert run.status == "FAILED" and run.failure_code == "CARGO_VEHICLE_OBSERVATION_REQUIRED"


async def test_crash_before_resource_commit_rolls_back_command_and_effect(
    database_session_factory, monkeypatch
):
    from tirodhan.modules.routing_operations import commands as module

    factory = database_session_factory
    _, _, _, point, _, _, execute = await setup(factory)
    command = VehicleCreate(
        client_command_id=new_uuid7(),
        mass_capacity_g=1000,
        volume_capacity_ml=1000,
        profile_version="test-car",
        end_receiving_point_id=point.receiving_point_id,
    )
    original = module.complete_idempotency_record

    async def crash(*args, **kwargs):
        raise RuntimeError("simulated failure before commit")

    monkeypatch.setattr(module, "complete_idempotency_record", crash)
    with pytest.raises(RuntimeError):
        await execute(command)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(OperatingVehicle)) == 1
    monkeypatch.setattr(module, "complete_idempotency_record", original)
    result = await execute(command)
    assert await execute(command) == result
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(OperatingVehicle)) == 2


@pytest.mark.parametrize("change", ["booking_status", "pickup_rollback"])
async def test_physical_history_cannot_be_erased_by_other_lifecycle_state(
    database_session_factory, change
):
    factory = database_session_factory
    fixture, vehicle, _, _, _, config, execute = await setup(factory)
    assignment = await assign_group_manually(
        factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        factory, assignment_id=assignment.assignment_id, rider_id=fixture.rider_ids[0]
    )
    await record_pickup_attempt(
        factory,
        pickup_execution_id=fixture.pickup_ids[0][0],
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome="COLLECTED",
    )
    async with factory() as session:
        pickup = await session.get(PickupExecution, fixture.pickup_ids[0][0])
    await execute(
        CargoObserve(
            client_command_id=new_uuid7(), request_id=pickup.request_id, vehicle_id=vehicle
        )
    )
    async with factory() as session, session.begin():
        if change == "booking_status":
            await session.execute(
                update(CollectionRequest)
                .where(CollectionRequest.request_id == pickup.request_id)
                .values(status="CANCELLED")
            )
        else:
            await session.execute(
                update(PickupExecution)
                .where(PickupExecution.pickup_execution_id == pickup.pickup_execution_id)
                .values(status="ASSIGNED")
            )
    run = await process(factory, await requested(factory, fixture, config), config)
    if change == "pickup_rollback":
        assert run.status == "FAILED" and run.failure_code == "PICKUP_CUSTODY_INCONSISTENT"
    else:
        assert run.status == "SUCCEEDED"
        assert (
            PROBLEM_CODEC.validate_python(run.problem).vehicles[0].initial_cargo[0].lot_id
            == pickup.request_id
        )
        async with factory() as session:
            request = await session.get(CollectionRequest, pickup.request_id)
            assert request.status == "CANCELLED"


async def test_two_waiting_closures_preserve_first_timestamp_and_revision(database_session_factory):
    factory = database_session_factory
    fixture, vehicle, shift, _, _, config, _ = await setup(factory)
    async with factory() as blocker:
        await blocker.begin()
        previous = await blocker.scalar(
            select(OperatingVehicle.version)
            .where(OperatingVehicle.vehicle_id == vehicle)
            .with_for_update()
        )

        async def close(index):
            return await execute_resource_command(
                factory,
                manager_id=fixture.manager_id,
                command=ShiftClose(client_command_id=new_uuid7(), shift_id=shift),
                expires_at=config.horizon_end,
                now=config.epoch + timedelta(seconds=index),
            )

        tasks = [asyncio.create_task(close(index)) for index in range(2)]
        try:
            # One closure can wait on the vehicle while its peer waits on the common
            # rider resource it holds. Both must be live PostgreSQL lock waiters.
            async def waiting():
                for _ in range(500):
                    async with factory() as observer:
                        count = await observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND pid<>pg_backend_pid() AND wait_event_type='Lock' "
                                "AND xact_start IS NOT NULL"
                            ),
                        )
                    if count >= 2:
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("both closure sessions did not wait on shared resources")

            await asyncio.wait_for(waiting(), 5)
            await blocker.commit()
            assert await asyncio.gather(*tasks) == [shift, shift]
        finally:
            await blocker.rollback()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    async with factory() as session:
        assert (
            await session.scalar(
                select(OperatingVehicle.version).where(OperatingVehicle.vehicle_id == vehicle)
            )
            == previous + 1
        )
        closed = await session.get(OperatingShift, shift)
        assert closed.closed_at in (config.epoch, config.epoch + timedelta(seconds=1))


async def test_rejected_parent_handover_cannot_release_cargo_projection(database_session_factory):
    factory = database_session_factory
    fixture, vehicle, _, point, _, config, execute = await setup(factory)
    assignment = await assign_group_manually(
        factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        factory, assignment_id=assignment.assignment_id, rider_id=fixture.rider_ids[0]
    )
    await record_pickup_attempt(
        factory,
        pickup_execution_id=fixture.pickup_ids[0][0],
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome="COLLECTED",
    )
    async with factory() as session:
        pickup = await session.get(PickupExecution, fixture.pickup_ids[0][0])
    await execute(
        CargoObserve(
            client_command_id=new_uuid7(), request_id=pickup.request_id, vehicle_id=vehicle
        )
    )
    await record_handover(
        factory,
        rider_id=fixture.rider_ids[0],
        client_handover_id=new_uuid7(),
        receiving_point_id=point.receiving_point_id,
        pickup_execution_ids=(fixture.pickup_ids[0][0],),
        observed_location=DELHI,
        idempotency_expires_at=config.horizon_end,
    )
    # Independent projection must fail conservatively on inconsistent legacy proof.
    async with factory() as session, session.begin():
        await session.execute(
            update(HandoverEvent).values(
                status="REJECTED", validation_code="OUTSIDE_ALLOWED_RADIUS"
            )
        )
    run = await process(factory, await requested(factory, fixture, config), config)
    assert run.status == "SUCCEEDED"
    assert (
        PROBLEM_CODEC.validate_python(run.problem).vehicles[0].initial_cargo[0].lot_id
        == pickup.request_id
    )
