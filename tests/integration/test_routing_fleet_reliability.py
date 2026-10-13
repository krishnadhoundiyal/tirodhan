from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from test_routing_dispatch import add_booking, collect_first, execute, root_revision
from test_routing_fleet import LineMatrix, engines, fixture, owners, tick

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem, RiderAvailability
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.planning.routing.contracts import OptimizerEngine
from tirodhan.modules.routing_operations.commands import (
    PositionReport,
    RestrictFacility,
    ShiftOpen,
    VehicleCreate,
)
from tirodhan.modules.routing_operations.dispatch_models import VehicleRoute
from tirodhan.modules.routing_operations.execution import DepartTask
from tirodhan.modules.routing_operations.fleet_models import FleetCycle, FleetException
from tirodhan.modules.routing_operations.models import CargoVehicleObservation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_crash_after_authority_commit_replays_without_duplicate_routes(
    database_session_factory, monkeypatch
):
    from tirodhan.modules.routing_operations import rolling

    factory = database_session_factory
    _, _, _, _, _, _, clock = await fixture(factory)
    original = rolling._finish

    async def crash(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(rolling, "_finish", crash)
    with pytest.raises(asyncio.CancelledError):
        await tick(factory, clock)
    async with factory() as session:
        cycle = await session.scalar(select(FleetCycle))
        assert cycle.status == "ACTIVATED" and cycle.activation_id is not None
        root, revision, _ = await root_revision(factory)
    monkeypatch.setattr(rolling, "_finish", original)
    clock[0] += timedelta(seconds=121)
    assert (await tick(factory, clock)).status == "UNCHANGED"
    assert (await root_revision(factory))[1].revision_id == revision.revision_id
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1
        assert (await session.get(VehicleRoute, root.route_id)).status == "ACTIVE"


async def test_rollback_before_authority_commit_leaves_no_partial_work(
    database_session_factory, monkeypatch
):
    from tirodhan.modules.routing_operations import authority

    factory = database_session_factory
    _, _, _, _, _, _, clock = await fixture(factory)
    original = authority.append_outbox_event

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated rollback")

    monkeypatch.setattr(authority, "append_outbox_event", crash)
    failed = await tick(factory, clock)
    assert failed.status == "FAILED" and failed.failure_code == "FLEET_EVALUATION_FAILED"
    assert not await owners(factory)
    monkeypatch.setattr(authority, "append_outbox_event", original)
    clock[0] += timedelta(seconds=1)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1


async def test_expired_owner_cannot_overwrite_successor_activation(database_session_factory):
    from tirodhan.modules.routing_operations.authority import RouteAuthorityError

    factory = database_session_factory
    _, _, _, _, _, _, clock = await fixture(factory)
    entered, release = asyncio.Event(), asyncio.Event()
    solvers = engines()
    original = solvers[OptimizerEngine.VROOM].propose

    async def paused(problem, *, search_seconds):
        entered.set()
        await release.wait()
        return await original(problem, search_seconds=search_seconds)

    solvers[OptimizerEngine.VROOM].propose = paused
    old = asyncio.create_task(tick(factory, clock, optimizers=solvers))
    await entered.wait()
    clock[0] += timedelta(seconds=121)
    successor = await tick(factory, clock)
    assert successor.status == "ACTIVATED"
    revision = (await root_revision(factory))[1]
    release.set()
    with pytest.raises(RouteAuthorityError, match="FLEET_LEASE_LOST"):
        await old
    assert (await root_revision(factory))[1].revision_id == revision.revision_id
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1


@pytest.mark.parametrize("conflict", ["capacity", "deadline", "shift"])
async def test_physical_conflict_never_erases_collected_custody(database_session_factory, conflict):
    from tirodhan.modules.routing_operations.models import OperatingShift, OperatingVehicle

    factory = database_session_factory
    f, vehicles, _, _, config, resource, clock = await fixture(factory, second=False)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, _, _, _, attempt, collected = await collect_first(factory, f, config)
    clock[0] = collected + timedelta(seconds=1)
    if conflict == "capacity":
        async with factory() as session, session.begin():
            await session.execute(
                update(OperatingVehicle).values(mass_capacity_g=1000, volume_capacity_ml=1000)
            )
    elif conflict == "deadline":
        clock[0] = config.epoch + timedelta(hours=3, seconds=1)
    else:
        async with factory() as session:
            shift = await session.scalar(
                select(OperatingShift).where(OperatingShift.vehicle_id == vehicles[0])
            )
        clock[0] = shift.ends_at + timedelta(seconds=1)
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
    failed = await tick(factory, clock)
    assert failed.status == "FAILED"
    async with factory() as session:
        assert (await session.get(VehicleRoute, root.route_id)).status == "RECOVERY_REQUIRED"
        assert (await session.scalar(select(CargoVehicleObservation))).pickup_attempt_id == attempt
        assert (await session.get(RiderAvailability, f.rider_ids[0])).work_state == "BUSY"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(FleetException)
                .where(FleetException.route_id == root.route_id)
            )
            >= 1
        )


async def test_nearest_baseline_is_measurably_worse_than_collective_allocation(
    database_session_factory,
):
    factory = database_session_factory
    _, _, _, _, _, _, clock = await fixture(factory, idle_position=(77.212, 28.632))
    result = await tick(factory, clock)
    assert result.status == "ACTIVATED", result.failure_code
    nearest = result.measurements["nearest_feasible_baseline"]
    selected = result.measurements[result.selected_engine]
    assert nearest["unserved_lots"] == selected["unserved_lots"] == 0
    assert nearest["additional_vehicles"] == 2 and selected["additional_vehicles"] == 1
    assert selected["generalized_seconds"] < nearest["generalized_seconds"]
    print("COLLECTIVE_VS_NEAREST", selected, nearest)


async def test_uncommitted_future_ownership_can_move_without_replacing_pickups(
    database_session_factory,
):
    factory = database_session_factory
    f, vehicles, _, ids, _, resource, clock = await fixture(factory)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    previous, _, _ = await root_revision(factory)
    clock[0] += timedelta(minutes=5)
    for vehicle, x in zip(vehicles, (77.225, 77.207), strict=True):
        await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicle,
                longitude=x,
                latitude=28.632,
                observed_at=clock[0],
            ),
            clock[0],
        )
    result = await tick(factory, clock)
    assert result.status == "ACTIVATED", result.failure_code
    assert set((await owners(factory)).values()) == {vehicles[1]}
    async with factory() as session:
        assert (await session.get(VehicleRoute, previous.route_id)).status == "CLOSED"
        assert await session.scalar(select(func.count()).select_from(PickupExecution)) == len(ids)
        assert await session.scalar(
            select(func.count())
            .select_from(RiderAssignmentItem)
            .where(RiderAssignmentItem.release_reason_code == "REASSIGNED")
        ) == len(ids)
        assert (await session.get(RiderAvailability, f.rider_ids[0])).work_state == "IDLE"


async def test_movement_after_road_refresh_retries_publication_without_resolving(
    database_session_factory,
):
    factory = database_session_factory
    _, vehicles, _, _, _, resource, clock = await fixture(factory)
    matrix = LineMatrix()
    original = matrix.acquire

    async def move_after_matrix(nodes):
        result = await original(nodes)
        if matrix.calls == 2:
            clock[0] += timedelta(seconds=1)
            await resource(
                PositionReport(
                    client_command_id=new_uuid7(),
                    vehicle_id=vehicles[0],
                    longitude=77.20703,
                    latitude=28.632,
                    observed_at=clock[0],
                ),
                clock[0],
            )
        return result

    matrix.acquire = move_after_matrix
    solvers = engines()
    result = await tick(factory, clock, matrix, solvers)
    assert result.status == "ACTIVATED", result.failure_code
    assert matrix.calls == 3 and all(s.calls == 1 for s in solvers.values())
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1


async def test_progress_during_solve_fences_old_business_snapshot(database_session_factory):
    factory = database_session_factory
    f, _, point, ids, config, _, clock = await fixture(factory, second=False)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    _, revision, tasks = await root_revision(factory)
    await add_booking(factory, config, point, ids[0], f.manager_id)
    clock[0] += timedelta(seconds=1)
    solvers = engines()
    original = solvers[OptimizerEngine.VROOM].propose

    async def depart_during_solve(problem, *, search_seconds):
        await execute(
            factory,
            f.rider_ids[0],
            DepartTask(
                client_command_id=new_uuid7(),
                task_id=tasks[0].task_id,
                revision_id=revision.revision_id,
            ),
            clock[0],
        )
        return await original(problem, search_seconds=search_seconds)

    solvers[OptimizerEngine.VROOM].propose = depart_during_solve
    result = await tick(factory, clock, optimizers=solvers)
    assert result.status == "STALE" and result.failure_code == "BUSINESS_SOURCE_CHANGED"
    assert (await root_revision(factory))[1].revision_id == revision.revision_id
    clock[0] += timedelta(seconds=1)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    assert (await root_revision(factory))[2][0].task_id == tasks[0].task_id


async def test_cancellation_and_refund_during_solve_remain_authoritative(database_session_factory):
    from tirodhan.modules.collection_requests.cancellation import (
        cancel_collection_request_by_customer,
    )
    from tirodhan.modules.payments.models import Refund

    factory = database_session_factory
    _, _, _, ids, _, _, clock = await fixture(factory)
    solvers = engines()
    original = solvers[OptimizerEngine.VROOM].propose

    async def cancel(problem, *, search_seconds):
        for key in ("fleet-race", "fleet-race", "fleet-race-second"):
            async with factory() as session, session.begin():
                booking = await session.get(CollectionRequest, ids[0])
                await cancel_collection_request_by_customer(
                    session,
                    request_id=ids[0],
                    customer_id=booking.customer_id,
                    idempotency_key=key,
                    planning_lead_time_minutes=1,
                    idempotency_expires_at=clock[0] + timedelta(days=1),
                )
        return await original(problem, search_seconds=search_seconds)

    solvers[OptimizerEngine.VROOM].propose = cancel
    result = await tick(factory, clock, optimizers=solvers)
    assert result.status == "STALE" and result.failure_code == "BUSINESS_SOURCE_CHANGED"
    assert not await owners(factory)
    async with factory() as session:
        assert (await session.get(CollectionRequest, ids[0])).status == "CANCELLED"
        assert await session.scalar(select(func.count()).select_from(Refund)) == 1
    clock[0] += timedelta(seconds=1)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    assert set(await owners(factory)) == {ids[1]}


async def test_two_simultaneous_workers_cannot_duplicate_authority(database_session_factory):
    factory = database_session_factory
    _, _, _, _, _, _, clock = await fixture(factory)
    results = await asyncio.gather(tick(factory, clock), tick(factory, clock))
    assert sum(r is not None and r.status == "ACTIVATED" for r in results) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(FleetCycle)) == 1
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1


async def test_insufficient_capacity_is_durable_backlog_and_later_capacity_recovers(
    database_session_factory,
):
    factory = database_session_factory
    f, _, point, ids, config, resource, clock = await fixture(factory, second=False)
    from tirodhan.modules.routing_operations.models import OperatingVehicle

    async with factory() as session, session.begin():
        await session.execute(
            update(OperatingVehicle).values(mass_capacity_g=1000, volume_capacity_ml=1000)
        )
    result = await tick(factory, clock)
    assert result.status == "UNCHANGED", result.failure_code
    assert not await owners(factory)
    async with factory() as session:
        assert set(
            await session.scalars(
                select(FleetException.request_id).where(
                    FleetException.reason == "CAPACITY_OR_WINDOW_SHORTFALL"
                )
            )
        ) == set(ids)
        assert {r.status for r in await session.scalars(select(CollectionRequest))} == {"ACCEPTED"}
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
    clock[0] += timedelta(seconds=1)
    await resource(
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=other,
            longitude=77.207,
            latitude=28.632,
            observed_at=clock[0],
        ),
        clock[0],
    )
    assert (await tick(factory, clock)).status == "ACTIVATED"
    assert set((await owners(factory)).values()) == {other}


@pytest.mark.parametrize("matrix_down", [False, True])
async def test_full_facility_holds_unsafe_route_without_erasing_cargo(
    database_session_factory, matrix_down
):
    from tirodhan.modules.planning.routing.execution import RoutingAdapterError

    factory = database_session_factory
    f, vehicles, point, _, config, resource, clock = await fixture(factory, second=matrix_down)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, _, _, _, attempt, collected = await collect_first(factory, f, config)
    clock[0] = collected + timedelta(seconds=1)
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
        RestrictFacility(
            client_command_id=new_uuid7(),
            receiving_point_id=point.receiving_point_id,
            starts_at=clock[0],
            ends_at=config.horizon_end,
            reason="FULL",
        ),
        clock[0],
    )

    class UnavailableMatrix(LineMatrix):
        async def acquire(self, nodes):
            raise RoutingAdapterError("OSRM_UNAVAILABLE")

    result = await tick(factory, clock, matrix=UnavailableMatrix() if matrix_down else None)
    assert result.status == "FAILED"
    assert result.failure_code == ("OSRM_UNAVAILABLE" if matrix_down else "NO_COMMITTED_CAPACITY")
    async with factory() as session:
        assert (await session.get(VehicleRoute, root.route_id)).status == "RECOVERY_REQUIRED"
        assert (await session.scalar(select(CargoVehicleObservation))).pickup_attempt_id == attempt
        assert (
            await session.scalar(
                select(func.count())
                .select_from(FleetException)
                .where(
                    FleetException.route_id == root.route_id,
                    FleetException.reason == "NO_AUTHORIZED_DISCHARGE_WINDOW",
                )
            )
            >= 1
        )


async def test_load_dispute_survives_release_and_cannot_be_cleared_by_old_manifest(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.commands import ClearVehicle, OperationalInputError
    from tirodhan.modules.routing_operations.recovery import RecoveryCommand, recover_route
    from tirodhan.modules.routing_operations.telemetry import (
        VehicleIssueCommand,
        report_vehicle_issue,
    )

    factory = database_session_factory
    f, vehicles, _, ids, _, resource, clock = await fixture(factory)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision, _ = await root_revision(factory)
    await report_vehicle_issue(
        factory,
        rider_id=f.rider_ids[0],
        command=VehicleIssueCommand(
            client_command_id=new_uuid7(), route_id=root.route_id, reason="LOAD_UNCERTAIN"
        ),
        expires_at=clock[0] + timedelta(days=1),
        now=clock[0],
    )
    with pytest.raises(OperationalInputError, match="AUDITED_LOAD_CORRECTION_REQUIRED"):
        await resource(ClearVehicle(client_command_id=new_uuid7(), vehicle_id=vehicles[0]))
    await recover_route(
        factory,
        manager_id=f.manager_id,
        command=RecoveryCommand(
            client_command_id=new_uuid7(),
            route_id=root.route_id,
            revision_id=revision.revision_id,
            reason_code="EXECUTION_EXCEPTION",
        ),
        expires_at=clock[0] + timedelta(days=1),
        now=clock[0],
    )
    clock[0] += timedelta(seconds=1)
    result = await tick(factory, clock)
    assert result.status == "UNCHANGED" and not await owners(factory)
    async with factory() as session:
        assert set(
            await session.scalars(
                select(FleetException.request_id).where(
                    FleetException.reason == "AUDITED_LOAD_CORRECTION_REQUIRED"
                )
            )
        ) == set(ids)
    import httpx
    from test_routing_operations import _headers, _token_codec, _token_for

    from tirodhan.core.config import Settings
    from tirodhan.main import create_app

    token, _ = await _token_for(factory, f.manager_id, roles=("MANAGER",))
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            routing_fleet_enabled=True,
            routing_dispatch_enabled=True,
        ),
        access_token_codec=_token_codec(),
    )
    app.state.database_session_factory = factory
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as api:
        response = await api.get("/v2/manager/fleet/backlog", headers=_headers(token))
    assert response.status_code == 200
    assert {item["request_id"] for item in response.json()["items"]} == {str(i) for i in ids}
    assert all(
        item["load_uncertain"] and not item["manifest_ready"] for item in response.json()["items"]
    )


async def test_fleet_api_flags_live_authorization_position_and_backlog(
    database_session_factory, monkeypatch
):
    import httpx
    from test_routing_fleet import policy
    from test_routing_operations import _headers, _token_codec, _token_for

    from tirodhan.core.config import Settings
    from tirodhan.main import create_app
    from tirodhan.modules.routing_operations import telemetry
    from tirodhan.modules.routing_operations.models import OperatingShift
    from tirodhan.modules.routing_operations.rolling import ConfigureFleet
    from tirodhan.modules.routing_operations.telemetry import PositionCommand

    factory = database_session_factory
    f, vehicles, _, ids, _, _, clock = await fixture(factory)
    manager_token, _ = await _token_for(factory, f.manager_id, roles=("MANAGER",))
    rider_token, _ = await _token_for(factory, f.rider_ids[0], roles=("RIDER",))
    closed = create_app(
        Settings(_env_file=None, environment="test"), access_token_codec=_token_codec()
    )
    closed.state.database_session_factory = factory
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=closed), base_url="https://test"
    ) as api:
        assert (
            await api.get("/v2/manager/fleet/backlog", headers=_headers(manager_token))
        ).status_code == 404
    with pytest.raises(ValueError, match="requires route execution"):
        create_app(Settings(_env_file=None, environment="test", routing_fleet_enabled=True))
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            routing_fleet_enabled=True,
            routing_dispatch_enabled=True,
            command_idempotency_ttl_seconds=86400,
        ),
        access_token_codec=_token_codec(),
    )
    app.state.database_session_factory = factory
    monkeypatch.setattr(telemetry, "utc_now", lambda: clock[0])
    clock[0] += timedelta(seconds=2)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as api:
        assert (
            await api.get("/v2/manager/fleet/status", headers=_headers(rider_token))
        ).status_code == 403
        first = await api.get("/v2/manager/fleet/backlog?limit=1", headers=_headers(manager_token))
        assert first.status_code == 200 and len(first.json()["items"]) == 1
        last = await api.get(
            "/v2/manager/fleet/backlog",
            params={"after_request_id": first.json()["next_request_id"]},
            headers=_headers(manager_token),
        )
        assert len(last.json()["items"]) == 1
        assert {first.json()["items"][0]["request_id"], last.json()["items"][0]["request_id"]} == {
            str(i) for i in ids
        }
        assert "longitude" not in first.text and "address" not in first.text
        command = ConfigureFleet(client_command_id=new_uuid7(), expected_version=1, policy=policy())
        value = await api.post(
            "/v2/manager/fleet/policy",
            json=command.model_dump(mode="json"),
            headers=_headers(manager_token),
        )
        assert value.status_code == 200, value.text
        async with factory() as session:
            shift = await session.scalar(
                select(OperatingShift).where(OperatingShift.vehicle_id == vehicles[0])
            )
        position = PositionCommand(
            client_command_id=new_uuid7(),
            shift_id=shift.shift_id,
            longitude=77.20701,
            latitude=28.632,
            observed_at=clock[0],
            accuracy_m=5,
        )
        value = await api.post(
            "/v2/rider/vehicle-position",
            json=position.model_dump(mode="json"),
            headers=_headers(rider_token),
        )
        assert value.status_code == 200, value.text
        assert (
            await api.post(
                "/v2/rider/vehicle-position",
                json=position.model_dump(mode="json"),
                headers=_headers(rider_token),
            )
        ).json() == value.json()
