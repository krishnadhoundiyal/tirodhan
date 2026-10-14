from __future__ import annotations

import math
import sys
from typing import Any

from .contracts import OptimizerEngine, RouteProposal, RoutingProblem
from .execution import RoutingAdapterError, decode_object, run_native
from .preparation import PreparedProblem, finish, prepare, residual_cost

VROOM_VERSION = "1.15.0"
ORTOOLS_VERSION = "9.14.6206"


class _Adapter:
    def __init__(
        self,
        *,
        overhead_seconds: float = 10,
        max_output_bytes: int = 4_000_000,
        max_search_seconds: int = 30,
    ) -> None:
        if (
            not math.isfinite(overhead_seconds)
            or overhead_seconds <= 0
            or type(max_output_bytes) is not int
            or max_output_bytes < 1
            or type(max_search_seconds) is not int
            or max_search_seconds < 1
        ):
            raise RoutingAdapterError("INVALID_SOLVER_CONFIGURATION")
        self.overhead, self.max_bytes, self.max_search = (
            overhead_seconds,
            max_output_bytes,
            max_search_seconds,
        )

    def prepared(self, problem: RoutingProblem, search_seconds: int) -> PreparedProblem:
        if type(search_seconds) is not int or not 1 <= search_seconds <= self.max_search:
            raise RoutingAdapterError("INVALID_SEARCH_BUDGET")
        return prepare(problem)


class ORToolsOptimizer(_Adapter):
    def __init__(
        self,
        *,
        python_executable: str = sys.executable,
        overhead_seconds: float = 10,
        max_output_bytes: int = 4_000_000,
        max_search_seconds: int = 30,
    ) -> None:
        super().__init__(
            overhead_seconds=overhead_seconds,
            max_output_bytes=max_output_bytes,
            max_search_seconds=max_search_seconds,
        )
        self.python = python_executable

    async def propose(self, problem: RoutingProblem, *, search_seconds: int) -> RouteProposal:
        prepared = self.prepared(problem, search_seconds)
        if not problem.stops and not problem.lots:
            return finish(problem, prepared, [], OptimizerEngine.OR_TOOLS, ORTOOLS_VERSION)
        raw = await run_native(
            (self.python, "-m", "tirodhan.modules.planning.routing.ortools_worker"),
            dict(prepared.payload, search_seconds=search_seconds),
            timeout_seconds=search_seconds + self.overhead,
            max_output_bytes=self.max_bytes,
        )
        result = decode_object(raw)
        if result.get("error"):
            code = result["error"]
            if code not in (
                "SOLVER_VERSION_MISMATCH",
                "SOLVER_UNAVAILABLE",
                "NO_FEASIBLE_PROPOSAL",
            ):
                code = "SOLVER_FAILED"
            raise RoutingAdapterError(code)
        routes = result.get("routes")
        if not isinstance(routes, list):
            raise RoutingAdapterError("INVALID_RESPONSE")
        candidate = finish(problem, prepared, routes, OptimizerEngine.OR_TOOLS, ORTOOLS_VERSION)
        served = {job for route in routes for job in route["jobs"]}
        expected = residual_cost(prepared, routes) + prepared.payload["penalty"] * sum(
            not item["required"] and item["id"] not in served for item in prepared.payload["stops"]
        )
        if type(result.get("objective")) is not int or result["objective"] != expected:
            raise RoutingAdapterError("OBJECTIVE_MISMATCH")
        return candidate


class VroomOptimizer(_Adapter):
    def __init__(
        self,
        *,
        binary: str = "vroom",
        overhead_seconds: float = 10,
        max_output_bytes: int = 4_000_000,
        max_search_seconds: int = 30,
    ) -> None:
        super().__init__(
            overhead_seconds=overhead_seconds,
            max_output_bytes=max_output_bytes,
            max_search_seconds=max_search_seconds,
        )
        self.binary = binary

    async def propose(self, problem: RoutingProblem, *, search_seconds: int) -> RouteProposal:
        prepared = self.prepared(problem, search_seconds)
        if not problem.stops and not problem.lots:
            return finish(problem, prepared, [], OptimizerEngine.VROOM, VROOM_VERSION)
        payload = vroom_payload(prepared)
        version = await run_native(
            (self.binary, "-v"), None, timeout_seconds=self.overhead, max_output_bytes=4096
        )
        if version.strip() != f"vroom {VROOM_VERSION}".encode():
            raise RoutingAdapterError("SOLVER_VERSION_MISMATCH")
        if not payload["jobs"] and not payload["shipments"]:
            return finish(problem, prepared, [], OptimizerEngine.VROOM, VROOM_VERSION)
        raw = await run_native(
            (self.binary, "-t", "1", "-x", "5", "-l", str(search_seconds)),
            payload,
            timeout_seconds=search_seconds + self.overhead,
            max_output_bytes=self.max_bytes,
        )
        result = decode_object(raw)
        try:
            if result["code"] != 0:
                raise ValueError("native error")
            routes = [
                dict(
                    vehicle=route["vehicle"],
                    jobs=[
                        step["id"]
                        for step in route["steps"]
                        if step["type"] in ("job", "pickup", "delivery")
                    ],
                )
                for route in result["routes"]
            ]
            cost = result["summary"]["cost"]
        except (KeyError, TypeError, ValueError):
            raise RoutingAdapterError("INVALID_RESPONSE") from None
        candidate = finish(problem, prepared, routes, OptimizerEngine.VROOM, VROOM_VERSION)
        if type(cost) is not int or cost != residual_cost(prepared, routes):
            raise RoutingAdapterError("OBJECTIVE_MISMATCH")
        return candidate


def vroom_payload(prepared: PreparedProblem) -> dict[str, Any]:
    p = prepared.payload
    stops = p["stops"]
    vehicles = p["vehicles"]
    payload: dict[str, Any] = dict(jobs=[], shipments=[], vehicles=[], matrices={})
    skills: dict[int, set[int]] = {v["id"]: set() for v in vehicles}

    def step(stop: dict[str, Any]) -> dict[str, Any]:
        return dict(
            id=stop["id"],
            location_index=stop["node"],
            service=stop["service"],
            time_windows=stop["windows"],
        )

    for lot in sorted({stop["lot"] for stop in stops}):
        group = [stop for stop in stops if stop["lot"] == lot]
        pickups = [stop for stop in group if stop["kind"] == "PICKUP"]
        drops = [stop for stop in group if stop["kind"] == "DISCHARGE"]
        if pickups:
            if len(drops) != 1 or drops[0]["quantity"] != pickups[0]["quantity"]:
                raise RoutingAdapterError("VROOM_SPLIT_NEW_LOT_UNSUPPORTED")
            pickup, delivery = pickups[0], drops[0]
            allowed = set(pickup["allowed"]) & set(delivery["allowed"])
            if not allowed or not pickup["windows"] or not delivery["windows"]:
                if pickup["required"] or delivery["required"]:
                    raise RoutingAdapterError("NO_FEASIBLE_PROPOSAL")
                continue
            skill = pickup["id"]
            for vi in allowed:
                skills[vi].add(skill)
            payload["shipments"].append(
                dict(
                    pickup=step(pickup),
                    delivery=step(delivery),
                    amount=pickup["delta"],
                    skills=[skill],
                    priority=100 if pickup["required"] or delivery["required"] else 0,
                )
            )
        else:
            owners = [v for v in vehicles if v["initial"][p["physical_dimensions"] + lot] > 0]
            if (
                len(owners) != 1
                or sum(stop["quantity"] for stop in drops)
                != owners[0]["initial"][p["physical_dimensions"] + lot]
            ):
                raise RoutingAdapterError("VROOM_CARGO_BALANCE_UNSUPPORTED")
            for stop in drops:
                vi = owners[0]["id"]
                if vi not in stop["allowed"] or not stop["windows"]:
                    raise RoutingAdapterError("NO_FEASIBLE_PROPOSAL")
                skills[vi].add(stop["id"])
                payload["jobs"].append(
                    dict(
                        step(stop),
                        delivery=[-x for x in stop["delta"]],
                        skills=[stop["id"]],
                        priority=100,
                    )
                )
    for v in vehicles:
        payload["vehicles"].append(
            dict(
                id=v["id"],
                profile=v["profile"],
                start_index=v["start"],
                end_index=v["end"],
                time_window=[v["available"], v["end_at"]],
                breaks=[
                    dict(
                        id=100000 + v["id"] * 100 + i,
                        time_windows=[[max(a, v["available"]), max(a, v["available"])]],
                        service=b - max(a, v["available"]),
                    )
                    for i, (a, b) in enumerate(v.get("breaks", []))
                ],
                capacity=v["capacity"],
                skills=sorted(skills[v["id"]]),
                costs=dict(fixed=0, per_hour=3600, per_km=0),
            )
        )
    for key, matrix in p["matrices"].items():
        payload["matrices"][key] = dict(matrix, costs=matrix["durations"])
    return payload
