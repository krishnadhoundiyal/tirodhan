import asyncio
import os
import time
from dataclasses import replace
from uuid import UUID

import httpx
import pytest

from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import ResourceKind, TimeWindow
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider, RoadNode
from tirodhan.modules.planning.routing.validation import validate_proposal

pytestmark = [
    pytest.mark.routing_engine,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1",
        reason="explicit opt-in pinned native routing environment required",
    ),
]


def optimizer(engine):
    return VroomOptimizer() if engine == "vroom" else ORToolsOptimizer()


@pytest.mark.parametrize("engine", ["vroom", "ortools"])
async def test_real_engine_initial_cargo_partial_discharge_and_protected_prefix(
    routing_problem, engine
):
    before = repr(routing_problem)
    candidate = await optimizer(engine).propose(routing_problem, search_seconds=1)
    assessment = validate_proposal(routing_problem, candidate)
    assert candidate.routes[0].stop_ids[0] == UUID(int=50)
    assert assessment.routes[0].stops[-1].load_after == (0, 0)
    assert assessment.routes[0].travel_seconds == 40
    assert repr(routing_problem) == before


@pytest.mark.parametrize("engine", ["vroom", "ortools"])
async def test_prefix_can_include_pickup_without_forgetting_its_cargo(routing_problem, engine):
    vehicle = replace(routing_problem.vehicles[0], protected_prefix=(UUID(int=50), UUID(int=51)))
    problem = replace(routing_problem, vehicles=(vehicle,))
    result = await optimizer(engine).propose(problem, search_seconds=1)
    assert result.routes[0].stop_ids[:2] == vehicle.protected_prefix
    validate_proposal(problem, result)


@pytest.mark.parametrize("engine", ["vroom", "ortools"])
async def test_required_impossible_work_does_not_produce_valid_proposal(routing_problem, engine):
    stops = tuple(
        replace(stop, service_start_window=TimeWindow(0, 0)) if stop.kind == "PICKUP" else stop
        for stop in routing_problem.stops
    )
    problem = replace(routing_problem, stops=stops)
    with pytest.raises(RoutingAdapterError):
        await optimizer(engine).propose(problem, search_seconds=1)
    # Same resource remains usable after the failed solve; no mutation/recovery write.
    validate_proposal(
        routing_problem, await optimizer(engine).propose(routing_problem, search_seconds=1)
    )


@pytest.mark.parametrize("engine", ["vroom", "ortools"])
async def test_optional_unreachable_work_is_explicitly_unserved(routing_problem, engine):
    matrix = routing_problem.matrices[0]
    values = tuple(
        tuple(None if (i != j and (i == 2 or j == 2)) else value for j, value in enumerate(row))
        for i, row in enumerate(matrix.travel_seconds)
    )
    stops = tuple(
        replace(stop, required=False, bound_vehicle_id=None)
        if stop.lot_id == UUID(int=42)
        else stop
        for stop in routing_problem.stops
    )
    problem = replace(
        routing_problem,
        stops=stops,
        matrices=(replace(matrix, travel_seconds=values, distance_m=values),),
    )
    result = await optimizer(engine).propose(problem, search_seconds=1)
    assert set(result.unserved_stop_ids) == {UUID(int=51), UUID(int=53)}
    validate_proposal(problem, result)


async def test_both_real_engines_evaluate_same_snapshot_concurrently(routing_problem):
    results = await asyncio.gather(
        *(
            optimizer(engine).propose(routing_problem, search_seconds=1)
            for engine in ("vroom", "ortools")
        )
    )
    assert all(validate_proposal(routing_problem, result) for result in results)
    assert results[0].snapshot_id == results[1].snapshot_id


async def test_ortools_specified_split_discharge_of_new_pickup(routing_problem):
    first = replace(routing_problem.stops[3], quantity=1)
    second = replace(first, stop_id=UUID(int=54), quantity=2)
    problem = replace(routing_problem, stops=routing_problem.stops[:3] + (first, second))
    candidate = await ORToolsOptimizer().propose(problem, search_seconds=1)
    validate_proposal(problem, candidate)
    with pytest.raises(RoutingAdapterError, match="VROOM_SPLIT_NEW_LOT_UNSUPPORTED"):
        await VroomOptimizer().propose(problem, search_seconds=1)


@pytest.mark.parametrize("engine", ["vroom", "ortools"])
async def test_multiple_profiles_and_hard_ownership(routing_problem, engine):
    vehicle = replace(
        routing_problem.vehicles[0],
        vehicle_id=UUID(int=70),
        rider_id=UUID(int=71),
        operating_shift_id=UUID(int=72),
        matrix_id=UUID(int=73),
        initial_cargo=(),
        protected_prefix=(),
    )
    original = routing_problem.matrices[0]
    slower = tuple(tuple(0 if i == j else 20 for j in range(4)) for i in range(4))
    matrix = replace(
        original, matrix_id=vehicle.matrix_id, travel_seconds=slower, distance_m=slower
    )
    stops = tuple(
        replace(
            stop,
            eligible_vehicle_ids=(UUID(int=30), vehicle.vehicle_id),
            bound_vehicle_id=vehicle.vehicle_id,
        )
        if stop.lot_id == UUID(int=42)
        else stop
        for stop in routing_problem.stops
    )
    problem = replace(
        routing_problem,
        vehicles=routing_problem.vehicles + (vehicle,),
        matrices=(original, matrix),
        stops=stops,
    )
    candidate = await optimizer(engine).propose(problem, search_seconds=1)
    assessment = validate_proposal(problem, candidate)
    assert len(assessment.routes) == 2
    routes = {route.vehicle_id: route.stop_ids for route in candidate.routes}
    assert set(routes[vehicle.vehicle_id]) == {UUID(int=51), UUID(int=53)}
    assert set(routes[UUID(int=30)]) == {UUID(int=50), UUID(int=52)}
    assert sum(route.travel_seconds for route in assessment.routes) == 90


@pytest.mark.parametrize("engine", ["vroom", "ortools"])
async def test_facility_opening_gaps_are_hard_constraints(routing_problem, engine):
    point = replace(
        routing_problem.receiving_points[1],
        opening_windows=(TimeWindow(0, 50), TimeWindow(500, 1000)),
    )
    problem = replace(
        routing_problem, receiving_points=(routing_problem.receiving_points[0], point)
    )
    candidate = await optimizer(engine).propose(problem, search_seconds=1)
    assessment = validate_proposal(problem, candidate)
    discharge = next(stop for stop in assessment.routes[0].stops if stop.stop_id == UUID(int=53))
    assert discharge.service_start_at >= 500


@pytest.mark.parametrize("engine", ["vroom", "ortools"])
async def test_required_pickup_cannot_exceed_physical_capacity(routing_problem, engine):
    lot = replace(routing_problem.lots[1], unit_load=(10, 20))
    problem = replace(routing_problem, lots=(routing_problem.lots[0], lot))
    with pytest.raises(RoutingAdapterError):
        await optimizer(engine).propose(problem, search_seconds=1)


@pytest.mark.parametrize("engine", ["vroom", "ortools"])
async def test_real_osrm_matrix_to_real_optimizer(routing_problem, engine):
    # Public road-network locations; synthetic demand/facilities/capacities only.
    coordinates = ((77.207, 28.632), (77.208, 28.631), (77.210, 28.631), (77.211, 28.630))
    nodes = tuple(RoadNode(UUID(int=i), lon, lat) for i, (lon, lat) in enumerate(coordinates))
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0), trust_env=False
    ) as client:
        matrix = await OSRMMatrixProvider(
            client=client,
            endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
            graph_version=os.environ["TIRODHAN_OSRM_GRAPH_VERSION"],
            profile_version="osrm-6.0.0-car",
            max_nodes=20,
            max_snap_distance_m=300,
        ).acquire(nodes)
    stops = tuple(
        replace(
            stop,
            service_start_window=TimeWindow(1000, 3000)
            if stop.kind == "PICKUP"
            else TimeWindow(0, 7000),
        )
        for stop in routing_problem.stops
    )
    vehicle = replace(
        routing_problem.vehicles[0],
        matrix_id=matrix.matrix_id,
        resource_kind=ResourceKind.FLEXIBLE,
        operating_shift_id=None,
        shift=TimeWindow(0, 7200),
    )
    problem = replace(
        routing_problem,
        matrices=(matrix,),
        vehicles=(vehicle,),
        stops=stops,
        lots=tuple(replace(lot, discharge_deadline=7000) for lot in routing_problem.lots),
        receiving_points=tuple(
            replace(point, opening_windows=(TimeWindow(0, 7200),))
            for point in routing_problem.receiving_points
        ),
    )
    started = time.perf_counter()
    candidate = await optimizer(engine).propose(problem, search_seconds=1)
    assessment = validate_proposal(problem, candidate)
    print(
        dict(
            engine=engine,
            seconds=round(time.perf_counter() - started, 3),
            travel_seconds=sum(route.travel_seconds for route in assessment.routes),
            distance_m=sum(route.distance_m for route in assessment.routes),
        )
    )
    assert not assessment.unserved_stop_ids
    assert candidate.routes[0].stop_ids[0] == UUID(int=50)
