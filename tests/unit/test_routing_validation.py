from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from uuid import UUID

import pytest

from tirodhan.modules.planning.routing.contracts import (
    ROUTING_CONTRACT_V1,
    CargoBalance,
    CargoLot,
    OptimizerEngine,
    ProposedRoute,
    ReceivingPointSnapshot,
    ResourceKind,
    RoadMatrix,
    RouteProposal,
    RoutingLimits,
    RoutingProblem,
    RoutingStop,
    StopKind,
    TimeWindow,
    VehicleSnapshot,
)
from tirodhan.modules.planning.routing.validation import (
    RoutingValidationError,
    validate_problem,
    validate_proposal,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


@pytest.fixture
def problem() -> RoutingProblem:
    nodes = tuple(uid(i) for i in range(4))
    matrix = tuple(tuple(0 if i == j else 10 for j in range(4)) for i in range(4))
    return RoutingProblem(
        problem_id=uid(1000),
        snapshot_id=uid(1001),
        contract_version=ROUTING_CONTRACT_V1,
        policy_version="test-policy-1",
        epoch=datetime(2026, 10, 12, tzinfo=timezone.utc),
        load_dimensions=("grams", "milliliters"),
        limits=RoutingLimits(20, 20, 5, 5),
        matrices=(RoadMatrix(uid(20), "graph-1", "profile-1", nodes, matrix, matrix),),
        vehicles=(
            VehicleSnapshot(
                uid(30),
                uid(31),
                7,
                ResourceKind.COMMITTED,
                uid(32),
                uid(20),
                uid(0),
                uid(0),
                0,
                TimeWindow(0, 1000),
                (24, 36),
                (CargoBalance(uid(40), 10),),
                (uid(50),),
            ),
        ),
        receiving_points=(
            ReceivingPointSnapshot(uid(60), uid(1), 2, True, (TimeWindow(0, 1000),)),
            ReceivingPointSnapshot(uid(61), uid(3), 2, True, (TimeWindow(0, 1000),)),
        ),
        lots=(
            CargoLot(
                uid(40), uid(41), 10, "accounting-unit", (2, 3), (uid(60), uid(61)), 900, None
            ),
            CargoLot(uid(42), uid(43), 3, "accounting-unit", (2, 3), (uid(61),), 900, uid(51)),
        ),
        stops=(
            RoutingStop(
                uid(50),
                uid(1),
                StopKind.DISCHARGE,
                uid(40),
                4,
                5,
                TimeWindow(0, 900),
                True,
                (uid(30),),
                uid(30),
                uid(60),
            ),
            RoutingStop(
                uid(51),
                uid(2),
                StopKind.PICKUP,
                uid(42),
                3,
                120,
                TimeWindow(100, 110),
                True,
                (uid(30),),
                uid(30),
                None,
            ),
            RoutingStop(
                uid(52),
                uid(3),
                StopKind.DISCHARGE,
                uid(40),
                6,
                5,
                TimeWindow(0, 900),
                True,
                (uid(30),),
                uid(30),
                uid(61),
            ),
            RoutingStop(
                uid(53),
                uid(3),
                StopKind.DISCHARGE,
                uid(42),
                3,
                5,
                TimeWindow(0, 900),
                True,
                (uid(30),),
                uid(30),
                uid(61),
            ),
        ),
    )


def proposal(
    problem: RoutingProblem, *, order: tuple[int, ...] = (50, 51, 52, 53)
) -> RouteProposal:
    return RouteProposal(
        problem.problem_id,
        problem.snapshot_id,
        problem.contract_version,
        problem.policy_version,
        OptimizerEngine.VROOM,
        "1.15.0",
        (ProposedRoute(uid(30), tuple(uid(i) for i in order)),),
        (),
    )


def change_stop(problem: RoutingProblem, index: int, **changes: object) -> RoutingProblem:
    stops = list(problem.stops)
    stops[index] = replace(stops[index], **changes)
    return replace(problem, stops=tuple(stops))


def expect_failure(problem: RoutingProblem, candidate: RouteProposal, code: str) -> None:
    with pytest.raises(RoutingValidationError) as error:
        validate_proposal(problem, candidate)
    assert error.value.code == code
    assert str(error.value) == code


@pytest.mark.parametrize("engine", tuple(OptimizerEngine))
def test_continuous_collection_conserves_partial_cargo_and_reconstructs_schedule(
    problem: RoutingProblem, engine: OptimizerEngine
) -> None:
    assessment = validate_proposal(problem, replace(proposal(problem), engine=engine))
    route = assessment.routes[0]
    assert [stop.load_after for stop in route.stops] == [(12, 18), (18, 27), (6, 9), (0, 0)]
    assert route.stops[1].arrival_at == 25
    assert route.stops[1].service_start_at == 100
    assert route.stops[1].service_end_at == 220  # Start is promised, not completion within slot.
    assert (route.end_at, route.travel_seconds, route.distance_m) == (250, 40, 40)
    assert route.resource_kind == ResourceKind.COMMITTED


def test_exact_replay_and_independent_proposals_have_no_mutation(problem: RoutingProblem) -> None:
    original = repr(problem)
    candidate = proposal(problem)
    assert validate_proposal(problem, candidate) == validate_proposal(problem, candidate)
    assert repr(problem) == original
    with pytest.raises(FrozenInstanceError):
        problem.snapshot_id = uid(999)  # type: ignore[misc]


def test_parallel_evaluation_has_no_shared_mutable_state(problem: RoutingProblem) -> None:
    candidate = proposal(problem)
    expected = validate_proposal(problem, candidate)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(executor.map(lambda _: validate_proposal(problem, candidate), range(40)))
    assert results == (expected,) * 40


def test_discharge_must_use_current_custodian_and_pickup_owner(problem: RoutingProblem) -> None:
    second = replace(
        problem.vehicles[0],
        vehicle_id=uid(33),
        rider_id=uid(34),
        initial_cargo=(),
        protected_prefix=(),
    )
    problem = replace(problem, vehicles=problem.vehicles + (second,))
    # Eligibility for another vehicle cannot erase the existing bound pickup owner.
    problem = change_stop(problem, 1, eligible_vehicle_ids=(uid(30), uid(33)))
    candidate = replace(
        proposal(problem, order=(50, 52)),
        routes=(
            ProposedRoute(uid(30), (uid(50), uid(52))),
            ProposedRoute(uid(33), (uid(51), uid(53))),
        ),
    )
    expect_failure(problem, candidate, "STOP_VEHICLE_INELIGIBLE")


def test_nested_mutable_matrix_is_rejected(problem: RoutingProblem) -> None:
    matrix = replace(problem.matrices[0], travel_seconds=list(problem.matrices[0].travel_seconds))
    with pytest.raises(RoutingValidationError, match="INVALID_MATRIX_SHAPE"):
        validate_problem(replace(problem, matrices=(matrix,)))


@pytest.mark.parametrize(
    "field", ["problem_id", "snapshot_id", "contract_version", "policy_version"]
)
def test_other_snapshot_or_contract_cannot_be_validated(
    problem: RoutingProblem, field: str
) -> None:
    value = uid(999) if field.endswith("_id") else "unsupported"
    expect_failure(
        problem, replace(proposal(problem), **{field: value}), "PROPOSAL_SNAPSHOT_MISMATCH"
    )


@pytest.mark.parametrize(
    ("order", "code"),
    [
        ((51, 50, 52, 53), "PROTECTED_PREFIX_CHANGED"),
        ((50, 51, 51, 52, 53), "DUPLICATE_ROUTE_STOP"),
        ((50, 51, 52, 999), "UNKNOWN_ROUTE_STOP"),
        ((50, 51, 52), "INCOMPLETE_COVERAGE"),
        ((50, 53, 51, 52), "CARGO_NOT_ONBOARD"),
    ],
)
def test_malformed_sequences_rejected(
    problem: RoutingProblem, order: tuple[int, ...], code: str
) -> None:
    expect_failure(problem, proposal(problem, order=order), code)


def test_duplicate_vehicle_route_rejected(problem: RoutingProblem) -> None:
    candidate = proposal(problem)
    expect_failure(
        problem, replace(candidate, routes=candidate.routes * 2), "DUPLICATE_ROUTE_VEHICLE"
    )


def test_duplicate_stop_across_routes_rejected(problem: RoutingProblem) -> None:
    vehicle = replace(
        problem.vehicles[0],
        vehicle_id=uid(33),
        rider_id=uid(34),
        initial_cargo=(),
        protected_prefix=(),
    )
    problem = replace(problem, vehicles=problem.vehicles + (vehicle,))
    candidate = proposal(problem)
    expect_failure(
        problem,
        replace(candidate, routes=candidate.routes + (ProposedRoute(uid(33), (uid(51),)),)),
        "DUPLICATE_ROUTE_STOP",
    )


def test_optional_work_must_be_explicitly_unserved(problem: RoutingProblem) -> None:
    problem = change_stop(
        change_stop(problem, 1, required=False, bound_vehicle_id=None),
        3,
        required=False,
        bound_vehicle_id=None,
    )
    candidate = replace(proposal(problem, order=(50, 52)), unserved_stop_ids=(uid(51), uid(53)))
    assessment = validate_proposal(problem, candidate)
    assert assessment.unserved_stop_ids == (uid(51), uid(53))
    expect_failure(problem, replace(candidate, unserved_stop_ids=()), "INCOMPLETE_COVERAGE")


def test_accepted_work_cannot_be_soft_dropped(problem: RoutingProblem) -> None:
    candidate = replace(proposal(problem, order=(50, 52)), unserved_stop_ids=(uid(51), uid(53)))
    expect_failure(problem, candidate, "REQUIRED_STOP_UNSERVED")


def test_stop_cannot_be_both_served_and_unserved(problem: RoutingProblem) -> None:
    expect_failure(
        problem, replace(proposal(problem), unserved_stop_ids=(uid(51),)), "SERVED_AND_UNSERVED"
    )


@pytest.mark.parametrize("dimensions", [(17, 36), (24, 26)])
def test_each_capacity_dimension_is_hard(
    problem: RoutingProblem, dimensions: tuple[int, int]
) -> None:
    # Initial cargo would exceed this capacity; remove it from the fixture by reducing unit load
    # for the initial lot. The new pickup must still overflow one dimension.
    lot = replace(problem.lots[0], unit_load=(1, 1))
    vehicle = replace(problem.vehicles[0], capacity=dimensions)
    problem = replace(problem, lots=(lot, problem.lots[1]), vehicles=(vehicle,))
    problem = replace(problem, lots=(lot, replace(problem.lots[1], unit_load=dimensions)))
    expect_failure(problem, proposal(problem), "CAPACITY_EXCEEDED")


@pytest.mark.parametrize(("index", "quantity"), [(0, 11), (2, 7)])
def test_over_discharge_rejected(problem: RoutingProblem, index: int, quantity: int) -> None:
    problem = change_stop(problem, index, quantity=quantity)
    expect_failure(problem, proposal(problem), "CARGO_NOT_ONBOARD")


def test_cargo_cannot_disappear_at_route_end(problem: RoutingProblem) -> None:
    problem = change_stop(problem, 2, quantity=5)
    expect_failure(problem, proposal(problem), "CARGO_NOT_DISCHARGED")


def test_initial_cargo_route_cannot_be_omitted(problem: RoutingProblem) -> None:
    # Remove demand so coverage is vacuous, but the vehicle still physically holds cargo.
    problem = replace(
        problem,
        stops=(),
        lots=(problem.lots[0],),
        vehicles=(replace(problem.vehicles[0], protected_prefix=()),),
    )
    candidate = replace(proposal(problem), routes=())
    expect_failure(problem, candidate, "COMMITTED_ROUTE_OMITTED")


def test_no_custody_transfer_between_vehicles(problem: RoutingProblem) -> None:
    second = replace(
        problem.vehicles[0],
        vehicle_id=uid(33),
        rider_id=uid(34),
        initial_cargo=(),
        protected_prefix=(),
    )
    problem = replace(problem, vehicles=problem.vehicles + (second,))
    problem = change_stop(
        problem, 2, eligible_vehicle_ids=(uid(30), uid(33)), bound_vehicle_id=None
    )
    candidate = replace(
        proposal(problem, order=(50, 51, 53)),
        routes=(
            ProposedRoute(uid(33), (uid(52),)),
            ProposedRoute(uid(30), (uid(50), uid(51), uid(53))),
        ),
    )
    expect_failure(problem, candidate, "CARGO_NOT_ONBOARD")


@pytest.mark.parametrize("change", ["unauthorized", "wrong-node", "unavailable", "hours"])
def test_receiving_point_is_authoritative(problem: RoutingProblem, change: str) -> None:
    if change == "unauthorized":
        problem = change_stop(problem, 3, receiving_point_id=uid(60), node_id=uid(1))
        code = "UNAUTHORIZED_RECEIVING_POINT"
    elif change == "wrong-node":
        problem = change_stop(problem, 3, node_id=uid(2))
        code = "WRONG_DISCHARGE_NODE"
    else:
        facility = replace(
            problem.receiving_points[1],
            available=change != "unavailable",
            opening_windows=(TimeWindow(0, 231),),
        )
        problem = replace(problem, receiving_points=(problem.receiving_points[0], facility))
        code = (
            "RECEIVING_POINT_UNAVAILABLE" if change == "unavailable" else "RECEIVING_POINT_CLOSED"
        )
    expect_failure(problem, proposal(problem), code)


def test_facility_waiting_uses_next_open_window(problem: RoutingProblem) -> None:
    facility = replace(
        problem.receiving_points[1], opening_windows=(TimeWindow(0, 231), TimeWindow(300, 500))
    )
    problem = replace(problem, receiving_points=(problem.receiving_points[0], facility))
    route = validate_proposal(problem, proposal(problem)).routes[0]
    assert (route.stops[2].arrival_at, route.stops[2].service_start_at) == (230, 300)
    assert route.end_at == 320


def test_custody_deadline_applies_to_discharge_completion(problem: RoutingProblem) -> None:
    problem = replace(
        problem, lots=(replace(problem.lots[0], discharge_deadline=232), problem.lots[1])
    )
    expect_failure(problem, proposal(problem), "CUSTODY_DEADLINE_MISSED")


def test_return_travel_is_included_in_shift(problem: RoutingProblem) -> None:
    problem = replace(problem, vehicles=(replace(problem.vehicles[0], shift=TimeWindow(0, 249)),))
    expect_failure(problem, proposal(problem), "SHIFT_END_MISSED")


def test_available_vehicle_can_still_miss_customer_window(problem: RoutingProblem) -> None:
    problem = replace(problem, vehicles=(replace(problem.vehicles[0], available_at=100),))
    expect_failure(problem, proposal(problem), "SERVICE_WINDOW_MISSED")


def test_matrix_direction_and_unreachable_arc_are_preserved(problem: RoutingProblem) -> None:
    matrix = problem.matrices[0]

    def changed(values: tuple[tuple[int | None, ...], ...], value: int | None):
        rows = [list(row) for row in values]
        rows[1][2] = value
        return tuple(tuple(row) for row in rows)

    asymmetric = replace(matrix, travel_seconds=changed(matrix.travel_seconds, 200))
    altered = replace(problem, matrices=(asymmetric,))
    expect_failure(altered, proposal(altered), "SERVICE_WINDOW_MISSED")
    inaccessible = replace(
        matrix,
        travel_seconds=changed(matrix.travel_seconds, None),
        distance_m=changed(matrix.distance_m, None),
    )
    altered = replace(problem, matrices=(inaccessible,))
    expect_failure(altered, proposal(altered), "UNREACHABLE_ARC")
    # An unused null arc must not exclude otherwise feasible demand.
    rows = [list(row) for row in matrix.travel_seconds]
    rows[2][1] = None
    values = tuple(tuple(row) for row in rows)
    altered = replace(
        problem, matrices=(replace(matrix, travel_seconds=values, distance_m=values),)
    )
    validate_proposal(altered, proposal(altered))


def test_flexible_proposal_remains_distinguishable_from_committed_capacity(
    problem: RoutingProblem,
) -> None:
    vehicle = replace(
        problem.vehicles[0], resource_kind=ResourceKind.FLEXIBLE, operating_shift_id=None
    )
    problem = replace(problem, vehicles=(vehicle,))
    assessment = validate_proposal(problem, proposal(problem))
    assert assessment.routes[0].resource_kind == ResourceKind.FLEXIBLE


@pytest.mark.parametrize(
    "case",
    [
        "version",
        "shift",
        "rider",
        "initial-owner",
        "lot-owner",
        "bound-owner",
        "prefix",
        "limit",
        "dimensions",
        "float",
        "bool",
        "shape",
        "epoch",
    ],
)
def test_invalid_snapshots_fail_closed(problem: RoutingProblem, case: str) -> None:
    vehicle = problem.vehicles[0]
    if case == "version":
        problem = replace(problem, contract_version="V99")
        code = "UNSUPPORTED_CONTRACT"
    elif case == "shift":
        problem = replace(problem, vehicles=(replace(vehicle, operating_shift_id=None),))
        code = "MISSING_COMMITTED_SHIFT"
    elif case == "rider":
        problem = replace(problem, vehicles=(vehicle, replace(vehicle, vehicle_id=uid(33))))
        code = "DUPLICATE_INPUT_ID"
    elif case == "initial-owner":
        second = replace(vehicle, vehicle_id=uid(33), rider_id=uid(34), protected_prefix=())
        problem = replace(problem, vehicles=(vehicle, second))
        code = "INVALID_INITIAL_CARGO"
    elif case == "lot-owner":
        problem = replace(problem, vehicles=(replace(vehicle, initial_cargo=()),))
        code = "MISSING_INITIAL_CARGO_OWNER"
    elif case == "bound-owner":
        problem = change_stop(problem, 1, bound_vehicle_id=uid(999))
        code = "INVALID_STOP_OWNER"
    elif case == "prefix":
        problem = replace(problem, vehicles=(replace(vehicle, protected_prefix=(uid(999),)),))
        code = "INVALID_PROTECTED_PREFIX"
    elif case == "limit":
        problem = replace(problem, limits=replace(problem.limits, max_stops=3))
        code = "WORKING_SET_LIMIT"
    elif case == "dimensions":
        problem = replace(problem, vehicles=(replace(vehicle, capacity=(24,)),))
        code = "INVALID_CAPACITY"
    elif case in ("float", "bool"):
        problem = change_stop(problem, 1, service_seconds=1.5 if case == "float" else True)
        code = "INVALID_STOP_TERMS"
    elif case == "shape":
        matrix = problem.matrices[0]
        problem = replace(
            problem, matrices=(replace(matrix, travel_seconds=matrix.travel_seconds[:2]),)
        )
        code = "INVALID_MATRIX_SHAPE"
    else:
        problem = replace(problem, epoch=problem.epoch.replace(tzinfo=None))
        code = "EPOCH_NOT_UTC"
    with pytest.raises(RoutingValidationError) as error:
        validate_problem(problem)
    assert error.value.code == code


def test_empty_scope_needs_neither_current_rider_nor_fictitious_capacity(
    problem: RoutingProblem,
) -> None:
    problem = replace(problem, vehicles=(), receiving_points=(), lots=(), stops=(), matrices=())
    candidate = replace(proposal(problem), routes=())
    assert validate_proposal(problem, candidate).routes == ()


def test_demand_without_supply_is_explicitly_infeasible(problem: RoutingProblem) -> None:
    problem = replace(
        problem,
        vehicles=(),
        lots=(problem.lots[1],),
        stops=tuple(
            replace(stop, eligible_vehicle_ids=(), bound_vehicle_id=None)
            for stop in (problem.stops[1], problem.stops[3])
        ),
    )
    candidate = replace(proposal(problem), routes=(), unserved_stop_ids=(uid(51), uid(53)))
    expect_failure(problem, candidate, "REQUIRED_STOP_UNSERVED")
