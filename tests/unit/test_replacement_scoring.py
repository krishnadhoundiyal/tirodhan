"""Counterexamples for bounded shortlist quality and comparable fleet impact."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from test_routing_validation import problem as problem_fixture
from test_routing_validation import proposal, uid

from tirodhan.modules.planning.routing.contracts import RoadMatrix, StopKind, TimeWindow
from tirodhan.modules.routing_operations.economics import EconomicPolicy, measure
from tirodhan.modules.routing_operations.replacement_options import incumbent_cost
from tirodhan.modules.routing_operations.replacement_scoring import impact_rank, ordered_vehicles


def measured_option(base_minutes, extra_minutes):
    problem = problem_fixture.__wrapped__()
    matrix = [[0 if i == j else 60 for j in range(4)] for i in range(4)]
    matrix[0][1] = base_minutes * 30
    matrix[1][2] = matrix[3][2] = base_minutes * 18
    matrix[2][0] = base_minutes * 12
    matrix[1][3] = extra_minutes * 60
    matrix = tuple(tuple(r) for r in matrix)
    problem = replace(
        problem,
        matrices=(
            RoadMatrix(
                uid(20), "synthetic", "test", tuple(uid(i) for i in range(4)), matrix, matrix
            ),
        ),
        vehicles=(
            replace(
                problem.vehicles[0],
                initial_cargo=(),
                protected_prefix=(),
                capacity=(100, 100),
                shift=TimeWindow(0, 20000),
            ),
        ),
        lots=(replace(problem.lots[0], pickup_stop_id=uid(50)), problem.lots[1]),
        receiving_points=tuple(
            replace(p, node_id=uid(2), opening_windows=(TimeWindow(0, 20000),))
            for p in problem.receiving_points
        ),
        stops=tuple(
            replace(
                s,
                node_id=uid(node),
                kind=kind,
                quantity=quantity,
                service_seconds=0,
                service_start_window=TimeWindow(0, 20000),
                receiving_point_id=point,
            )
            for s, node, kind, quantity, point in zip(
                problem.stops,
                (1, 3, 2, 2),
                (StopKind.PICKUP, StopKind.PICKUP, StopKind.DISCHARGE, StopKind.DISCHARGE),
                (10, 3, 10, 3),
                (None, None, uid(60), uid(61)),
                strict=True,
            )
        ),
    )
    problem = replace(
        problem, lots=tuple(replace(lot, discharge_deadline=20000) for lot in problem.lots)
    )
    policy = SimpleNamespace(
        economics=EconomicPolicy(
            version="test",
            additional_vehicle_seconds=0,
            disruption_seconds=0,
            waiting_weight=0,
            window_risk_buffer_seconds=0,
            window_risk_weight=0,
            max_candidate_evaluations=10,
        )
    )
    orders = {uid(30): (uid(50), uid(52))}
    baseline = incumbent_cost(problem, {uid(42)}, orders, policy)
    cost = measure(problem, proposal(problem), policy.economics, orders)
    return cost, baseline


def test_minimum_incremental_fleet_work_beats_shorter_total_candidate_route():
    a, old_a = measured_option(100, 4)
    b, old_b = measured_option(20, 17)
    assert (old_a.travel_seconds, a.travel_seconds) == (6000, 6240)
    assert (old_b.travel_seconds, b.travel_seconds) == (1200, 2220)
    assert b.rank < a.rank  # The old absolute comparison picks the wrong rider.
    assert impact_rank(a, old_a) < impact_rank(b, old_b)
    assert a.travel_seconds + old_b.travel_seconds < b.travel_seconds + old_a.travel_seconds


def test_generalized_impact_includes_risk_waiting_disruption_and_activation_costs():
    a, baseline = measured_option(100, 4)
    b, other = measured_option(20, 17)
    # A's shorter detour loses when its configured extra penalties exceed B's delta.
    penalized = replace(a, generalized_seconds=a.generalized_seconds + 1000)
    assert impact_rank(b, other) < impact_rank(penalized, baseline)
    assert (
        impact_rank(replace(a, generalized_seconds=baseline.generalized_seconds - 1), baseline)[1]
        == -1
    )


def test_missing_baseline_is_unpriced_not_zero_or_absolute_route_cost():
    a, baseline = measured_option(100, 4)
    b, _ = measured_option(20, 17)
    assert impact_rank(a, baseline) < impact_rank(b, None)
    assert impact_rank(a, None) == impact_rank(b, None)


NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def request(identity, longitude, start=1, end=2):
    return dict(
        request_id=uid(identity),
        longitude=longitude,
        latitude=28.6,
        slot_start=NOW + timedelta(hours=start),
        slot_end=NOW + timedelta(hours=end),
        receiving_point_id=uid(9000),
        discharge_deadline=NOW + timedelta(hours=6),
        pickup_status="ASSIGNED",
        cargo_vehicle_id=None,
    )


def vehicle(identity, longitude, **changes):
    return dict(
        vehicle_id=uid(identity),
        shift_id=uid(identity + 1000),
        resource_kind="COMMITTED",
        longitude=longitude,
        latitude=28.6,
        end_receiving_point_id=uid(9000),
        ends_at=NOW + timedelta(hours=6),
        **changes,
    )


def sources(vehicles, requests=(), routes=(), tasks=()):
    return dict(
        vehicles=vehicles,
        requests=requests,
        routes=routes,
        tasks=tasks,
        points=[dict(receiving_point_id=uid(9000), longitude=77.2, latitude=28.6)],
    )


def task(row, route, **changes):
    return dict(
        route_id=route,
        stop_id=uuid5(NAMESPACE_URL, f"tirodhan:pickup:{row['request_id']}"),
        state="PENDING",
        **changes,
    )


def test_hundred_candidates_ten_evaluation_budget_includes_rider_87():
    rescue = request(500, 77.2)
    fleet = [vehicle(i, 77.0) for i in range(1, 101)]
    fleet[86] = vehicle(87, 77.2)
    ordered = ordered_vehicles(sources(fleet), {rescue["request_id"]: rescue}, NOW)
    assert ordered[0]["vehicle_id"] == uid(87)
    assert uid(87) in {v["vehicle_id"] for v in ordered[:10]}
    assert len(ordered) == 100  # Ordering never becomes an eligibility/radius exclusion.
    assert ordered == ordered_vehicles(sources(list(reversed(fleet))), {uid(500): rescue}, NOW)


def test_farther_start_on_route_can_beat_nearer_rider_heading_away():
    rescue = request(500, 77.1)
    existing = request(501, 77.3)
    enroute = request(502, 77.19)
    fleet = [vehicle(1, 77.09), vehicle(2, 77.0)]
    # Rider 1's committed destination must be visited first. Rider 2 passes the rescue.
    data = sources(
        fleet,
        [existing, enroute],
        [dict(vehicle_id=uid(1), route_id=uid(700)), dict(vehicle_id=uid(2), route_id=uid(701))],
        [task(existing, uid(700), committed_at=NOW), task(enroute, uid(701))],
    )
    assert ordered_vehicles(data, {uid(500): rescue}, NOW)[0]["vehicle_id"] == uid(2)


def test_same_location_different_pickup_time_affects_shortlist():
    rescue = request(500, 77.1, start=1, end=1.5)
    existing = request(501, 77.1, start=4, end=4.5)
    fleet = [vehicle(1, 77.1), vehicle(2, 77.0)]
    data = sources(
        fleet,
        [existing],
        [dict(vehicle_id=uid(1), route_id=uid(700))],
        [task(existing, uid(700), committed_at=NOW)],
    )
    assert ordered_vehicles(data, {uid(500): rescue}, NOW)[0]["vehicle_id"] == uid(2)


def test_cargo_recovery_orders_by_donor_rendezvous_not_customer_address():
    rescue = dict(request(500, 77.0), pickup_status="COLLECTED", cargo_vehicle_id=uid(600))
    data = sources([vehicle(1, 77.0), vehicle(87, 77.2)])
    data["recovery_donor_positions"] = {str(uid(600)): dict(longitude=77.2, latitude=28.6)}
    assert ordered_vehicles(data, {uid(500): rescue}, NOW)[0]["vehicle_id"] == uid(87)


def test_committed_priority_and_identity_ties_are_preserved():
    rescue = request(500, 77.2)
    flexible = vehicle(1, 77.2)
    flexible["resource_kind"] = "FLEXIBLE"
    data = sources([flexible, vehicle(3, 77.0), vehicle(2, 77.0)])
    assert [v["vehicle_id"] for v in ordered_vehicles(data, {uid(500): rescue}, NOW)] == [
        uid(2),
        uid(3),
        uid(1),
    ]
