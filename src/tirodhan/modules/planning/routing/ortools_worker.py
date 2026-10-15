"""Private, finite native subprocess. Not an API, worker deployment or dispatch authority."""

from __future__ import annotations

import importlib.metadata
import json
import sys
from typing import Any


def solve(p: dict[str, Any]) -> dict[str, Any]:
    try:
        if importlib.metadata.version("ortools") != "9.14.6206":
            return {"error": "SOLVER_VERSION_MISMATCH"}
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except (ImportError, importlib.metadata.PackageNotFoundError):
        return {"error": "SOLVER_UNAVAILABLE"}
    jobs, vehicles = p["stops"], p["vehicles"]
    n, k = len(jobs), len(vehicles)
    if not k:
        return {"error": "NO_FEASIBLE_PROPOSAL"}
    nodes = (
        [j["node"] for j in jobs] + [v["start"] for v in vehicles] + [v["end"] for v in vehicles]
    )
    manager = pywrapcp.RoutingIndexManager(
        len(nodes), k, list(range(n, n + k)), list(range(n + k, n + 2 * k))
    )
    routing = pywrapcp.RoutingModel(manager)
    horizon = p["horizon"]
    callbacks = []
    for vi, vehicle in enumerate(vehicles):
        matrix = p["matrices"][vehicle["profile"]]["durations"]

        def cost(a: int, b: int, matrix: list[list[int]] = matrix, vi: int = vi) -> int:
            if a == routing.Start(vi) and b == routing.End(vi) and not vehicles[vi]["mandatory"]:
                return 0
            na, nb = manager.IndexToNode(a), manager.IndexToNode(b)
            if nodes[na] < 0 or nodes[nb] < 0:
                return int(horizon + 1)
            return int(matrix[nodes[na]][nodes[nb]])

        def travel(a: int, b: int, cost: Any = cost) -> int:
            na = manager.IndexToNode(a)
            return int(cost(a, b) + (jobs[na]["service"] if na < n else 0))

        routing.SetArcCostEvaluatorOfVehicle(routing.RegisterTransitCallback(cost), vi)
        callbacks.append(routing.RegisterTransitCallback(travel))
        if vehicle["mandatory"]:
            routing.SetVehicleUsedWhenEmpty(True, vi)
    routing.AddDimensionWithVehicleTransits(callbacks, horizon, horizon, False, "Time")
    time = routing.GetDimensionOrDie("Time")
    for vi, vehicle in enumerate(vehicles):
        time.CumulVar(routing.Start(vi)).SetRange(vehicle["available"], vehicle["end_at"])
        time.CumulVar(routing.End(vi)).SetRange(vehicle["available"], vehicle["end_at"])
        pauses = [
            routing.solver().FixedDurationIntervalVar(
                max(a, vehicle["available"]),
                max(a, vehicle["available"]),
                b - max(a, vehicle["available"]),
                False,
                f"break-{vi}-{i}",
            )
            for i, (a, b) in enumerate(vehicle.get("breaks", []))
        ]
        if pauses:
            time.SetBreakIntervalsOfVehicle(
                pauses,
                vi,
                [
                    jobs[manager.IndexToNode(i)]["service"] if manager.IndexToNode(i) < n else 0
                    for i in range(routing.Size())
                ],
            )
    dimensions = len(vehicles[0]["capacity"])
    for dimension in range(dimensions):

        def delta(a: int, dimension: int = dimension) -> int:
            na = manager.IndexToNode(a)
            return int(jobs[na]["delta"][dimension]) if na < n else 0

        name = f"Load{dimension}"
        routing.AddDimensionWithVehicleCapacity(
            routing.RegisterUnaryTransitCallback(delta),
            0,
            [v["capacity"][dimension] for v in vehicles],
            False,
            name,
        )
        load = routing.GetDimensionOrDie(name)
        for vi, vehicle in enumerate(vehicles):
            load.CumulVar(routing.Start(vi)).SetValue(vehicle["initial"][dimension])
            load.CumulVar(routing.End(vi)).SetValue(0)
    for ji, job in enumerate(jobs):
        index = manager.NodeToIndex(ji)
        eligible = [vi for vi, v in enumerate(vehicles) if v["id"] in job["allowed"]]
        windows = job["windows"]
        if not eligible or not windows:
            if job["required"]:
                return {"error": "NO_FEASIBLE_PROPOSAL"}
            routing.ActiveVar(index).SetValue(0)
        else:
            routing.SetAllowedVehiclesForIndex(eligible, index)
            time.CumulVar(index).SetRange(windows[0][0], windows[-1][1])
            for (_, previous_end), (next_start, _) in zip(windows, windows[1:], strict=False):
                if previous_end + 1 < next_start:
                    time.CumulVar(index).RemoveInterval(previous_end + 1, next_start - 1)
        if not job["required"]:
            routing.AddDisjunction([index], p["penalty"])
    # Whole new lots have native shipment pairs, improving insertion construction.
    # Split new lots use per-lot nonnegative load dimensions and empty ends instead.
    for lot in sorted({j["lot"] for j in jobs}):
        pickups = [i for i, j in enumerate(jobs) if j["lot"] == lot and j["kind"] == "PICKUP"]
        drops = [i for i, j in enumerate(jobs) if j["lot"] == lot and j["kind"] == "DISCHARGE"]
        if len(pickups) == len(drops) == 1:
            a, b = manager.NodeToIndex(pickups[0]), manager.NodeToIndex(drops[0])
            routing.AddPickupAndDelivery(a, b)
            routing.solver().Add(routing.VehicleVar(a) == routing.VehicleVar(b))
            routing.solver().Add(time.CumulVar(a) <= time.CumulVar(b))
            routing.solver().Add(routing.ActiveVar(a) == routing.ActiveVar(b))
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(p["search_seconds"])
    params.sat_parameters.random_seed = 1729
    solution = routing.SolveWithParameters(params)
    if solution is None:
        return {"error": "NO_FEASIBLE_PROPOSAL"}
    routes = []
    for vi, vehicle in enumerate(vehicles):
        index = routing.Start(vi)
        order = []
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node < n:
                order.append(jobs[node]["id"])
            index = solution.Value(routing.NextVar(index))
        if order or vehicle["mandatory"]:
            routes.append(dict(vehicle=vehicle["id"], jobs=order))
    return dict(routes=routes, objective=solution.ObjectiveValue())


def main() -> None:
    try:
        # Parent serializes an already bounded/validated internal model, never public input.
        p = json.loads(sys.stdin.buffer.read())
        result = solve(p)
    except Exception:
        result = {"error": "SOLVER_FAILED"}
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
