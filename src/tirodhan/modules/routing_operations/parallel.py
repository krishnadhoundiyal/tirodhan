"""Transportable solver tasks, validated complete routes and restricted CP-SAT selection.

No database access or assignment authority. Local multiprocessing is one task transport;
neither task nor result refers to coordinator paths, localhost services or shared memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from tirodhan.modules.planning.routing.adapters import (
    ORTOOLS_VERSION,
    VROOM_VERSION,
    ORToolsOptimizer,
    VroomOptimizer,
)
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ProposedRoute,
    RouteProposal,
    RoutingProblem,
    SelectionEngine,
    StopKind,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal

from .economics import EconomicPolicy, measure

VERSION = "REGIONAL_PARALLEL_V1"


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkerTask(Frozen):
    version: str = VERSION
    task_id: UUID
    engine: OptimizerEngine
    problem: RoutingProblem
    search_seconds: int = Field(ge=1, le=30)


class WorkerResult(Frozen):
    version: str = VERSION
    task_id: UUID
    proposal: RouteProposal | None = None
    failure: str | None = None
    elapsed_seconds: float


def solve_task(payload: bytes) -> bytes:
    """Pure process entry point; safe to move unchanged behind a future transport."""
    started = time.monotonic()
    task = WorkerTask.model_validate_json(payload)
    if task.version != VERSION:
        raise ValueError("WORKER_CONTRACT_VERSION")
    optimizer = VroomOptimizer() if task.engine == OptimizerEngine.VROOM else ORToolsOptimizer()
    try:
        proposal = asyncio.run(optimizer.propose(task.problem, search_seconds=task.search_seconds))
        validate_proposal(task.problem, proposal)
        if proposal.engine != task.engine:
            raise ValueError("WORKER_ENGINE_IDENTITY")
        result = WorkerResult(
            task_id=task.task_id, proposal=proposal, elapsed_seconds=time.monotonic() - started
        )
    except RoutingAdapterError as error:
        result = WorkerResult(
            task_id=task.task_id, failure=error.code, elapsed_seconds=time.monotonic() - started
        )
    except Exception:
        # Raw solver stderr/coordinates are never part of task diagnostics.
        result = WorkerResult(
            task_id=task.task_id,
            failure="SOLVER_FAILED_OR_NO_PROPOSAL",
            elapsed_seconds=time.monotonic() - started,
        )
    return result.model_dump_json().encode()


async def local_workers(tasks: list[WorkerTask], processes: int) -> list[WorkerResult]:
    if not 1 <= processes <= 16:
        raise ValueError("WORKER_COUNT")
    results: list[WorkerResult] = []
    # Bounded waves prevent an unbounded executor queue. Native subprocesses supervise
    # their own deadlines; shutdown waits for that finite cleanup, never abandons children.
    with ProcessPoolExecutor(
        max_workers=processes, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        loop = asyncio.get_running_loop()
        for start in range(0, len(tasks), processes):
            wave = tasks[start : start + processes]
            outputs = await asyncio.gather(
                *(
                    loop.run_in_executor(pool, solve_task, t.model_dump_json().encode())
                    for t in wave
                ),
                return_exceptions=True,
            )
            for task, output in zip(wave, outputs, strict=True):
                if isinstance(output, BaseException):
                    results.append(
                        WorkerResult(
                            task_id=task.task_id, failure="WORKER_PROCESS_FAILED", elapsed_seconds=0
                        )
                    )
                    continue
                result = WorkerResult.model_validate_json(output)
                if result.task_id != task.task_id or result.version != VERSION:
                    raise ValueError("WORKER_RESULT_IDENTITY")
                results.append(result)
    return results


class Candidate(Frozen):
    candidate_id: UUID
    vehicle_id: UUID
    rider_id: UUID
    stop_ids: tuple[UUID, ...]
    lot_ids: tuple[UUID, ...]
    pickup_ids: tuple[UUID, ...]
    construction: str
    engine_version: str
    parent_id: UUID | None = None
    cost: int
    supplementary_pickups: int = 0
    rebase_source_id: UUID | None = None
    rebase_snapshot_id: UUID | None = None
    # Fixed schedules: consolidation never changes a candidate's stop sequence/timing.
    facility_visits: tuple[tuple[UUID, int, int], ...] = ()


def route_problem(problem: RoutingProblem, route: ProposedRoute) -> RoutingProblem:
    vehicle = next(v for v in problem.vehicles if v.vehicle_id == route.vehicle_id)
    stops = tuple(
        replace(
            s,
            eligible_vehicle_ids=tuple(
                v for v in s.eligible_vehicle_ids if v == vehicle.vehicle_id
            ),
        )
        for s in problem.stops
        if s.stop_id in route.stop_ids
    )
    lots = {s.lot_id for s in stops} | {c.lot_id for c in vehicle.initial_cargo}
    return slice_problem(problem, lots, route.vehicle_id)


def slice_problem(problem: RoutingProblem, lot_ids: set[UUID], vehicle_id: UUID) -> RoutingProblem:
    return group_problem(problem, lot_ids, (vehicle_id,))


def group_problem(
    problem: RoutingProblem, lot_ids: set[UUID], vehicle_ids: tuple[UUID, ...]
) -> RoutingProblem:
    vehicles = tuple(v for v in problem.vehicles if v.vehicle_id in vehicle_ids)
    lots = tuple(lot for lot in problem.lots if lot.lot_id in lot_ids)
    stops = tuple(
        replace(
            stop,
            eligible_vehicle_ids=tuple(v for v in stop.eligible_vehicle_ids if v in vehicle_ids),
        )
        for stop in problem.stops
        if stop.lot_id in lot_ids
    )
    authorized = {point for lot in lots for point in lot.authorized_receiving_point_ids}
    points = tuple(
        point for point in problem.receiving_points if point.receiving_point_id in authorized
    )
    nodes = (
        {v.start_node_id for v in vehicles}
        | {v.end_node_id for v in vehicles}
        | {s.node_id for s in stops}
        | {point.node_id for point in points}
    )
    matrices = []
    replacements = {}
    for matrix in problem.matrices:
        if matrix.matrix_id not in {v.matrix_id for v in vehicles}:
            continue
        indexes = [i for i, node in enumerate(matrix.node_ids) if node in nodes]
        order = tuple(matrix.node_ids[i] for i in indexes)
        identity = uuid5(NAMESPACE_URL, f"{matrix.matrix_id}:{order}")
        replacements[matrix.matrix_id] = identity
        matrices.append(
            replace(
                matrix,
                matrix_id=identity,
                node_ids=order,
                travel_seconds=tuple(
                    tuple(matrix.travel_seconds[i][j] for j in indexes) for i in indexes
                ),
                distance_m=tuple(tuple(matrix.distance_m[i][j] for j in indexes) for i in indexes),
            )
        )
    return replace(
        problem,
        problem_id=uuid5(
            NAMESPACE_URL, f"{problem.problem_id}:{vehicle_ids}:{sorted(lot_ids, key=str)}"
        ),
        matrices=tuple(matrices),
        vehicles=tuple(replace(v, matrix_id=replacements[v.matrix_id]) for v in vehicles),
        receiving_points=points,
        lots=lots,
        stops=stops,
    )


def candidate(
    problem: RoutingProblem,
    route: ProposedRoute,
    construction: str,
    engine_version: str,
    economics: EconomicPolicy,
    incumbent: dict[UUID, tuple[UUID, ...]],
    parent: UUID | None = None,
) -> Candidate:
    local = route_problem(problem, route)
    proposal = RouteProposal(
        local.problem_id,
        local.snapshot_id,
        local.contract_version,
        local.policy_version,
        SelectionEngine.COMPOSITE,
        VERSION,
        (route,),
        (),
    )
    assessment = validate_proposal(local, proposal)
    measured = measure(
        local, proposal, economics, {route.vehicle_id: incumbent.get(route.vehicle_id, ())}
    )
    stops = {s.stop_id: s for s in local.stops}
    identity = f"{problem.snapshot_id}:{route.vehicle_id}:{route.stop_ids}:{construction}:{parent}"
    return Candidate(
        candidate_id=uuid5(NAMESPACE_URL, identity),
        vehicle_id=route.vehicle_id,
        rider_id=local.vehicles[0].rider_id,
        stop_ids=route.stop_ids,
        lot_ids=tuple(sorted((lot.lot_id for lot in local.lots), key=str)),
        pickup_ids=tuple(s.lot_id for s in local.stops if s.kind == StopKind.PICKUP),
        construction=construction,
        engine_version=engine_version,
        parent_id=parent,
        cost=measured.generalized_seconds,
        supplementary_pickups=measured.supplementary_lots,
        facility_visits=tuple(
            (cast(UUID, stops[s.stop_id].receiving_point_id), s.service_start_at, s.service_end_at)
            for s in assessment.routes[0].stops
            if stops[s.stop_id].receiving_point_id is not None
        ),
    )


def conflict_alternatives(
    problem: RoutingProblem,
    candidates: list[Candidate],
    economics: EconomicPolicy,
    incumbent: dict[UUID, tuple[UUID, ...]],
    budget: int,
) -> list[Candidate]:
    claims: dict[UUID, set[UUID]] = {}
    for c in candidates:
        for lot in c.pickup_ids:
            claims.setdefault(lot, set()).add(c.vehicle_id)
    protected = {s for v in problem.vehicles for s in v.protected_prefix}
    stops = {s.stop_id: s for s in problem.stops}
    mandatory = {s.lot_id for s in problem.stops if s.required}
    adjusted: list[Candidate] = []
    attempts = 0
    for original in candidates:
        for lot in sorted(original.pickup_ids, key=str):
            if attempts >= budget:
                return candidates + adjusted
            if len(claims[lot]) <= 1 or lot in mandatory:
                continue
            removed = {s for s in original.stop_ids if stops[s].lot_id == lot}
            if protected.intersection(removed):
                continue
            attempts += 1
            route = ProposedRoute(
                original.vehicle_id, tuple(s for s in original.stop_ids if s not in removed)
            )
            try:
                adjusted.append(
                    candidate(
                        problem,
                        route,
                        "CONTESTED_REMOVAL",
                        original.engine_version,
                        economics,
                        incumbent,
                        original.candidate_id,
                    )
                )
            except RoutingValidationError:
                continue
    return candidates + adjusted


class FacilityLimit(Frozen):
    receiving_point_id: UUID
    concurrent_unloads: int = Field(ge=1)


class Selection(Frozen):
    version: str = VERSION
    policy_version: str
    candidates: tuple[Candidate, ...]
    selected_ids: tuple[UUID, ...]
    stage_statuses: tuple[str, ...]
    stage_values: tuple[int, ...]
    stage_bounds: tuple[float, ...]
    elapsed_seconds: float
    pool_digest: str


def pool_digest(candidates: tuple[Candidate, ...]) -> str:
    return hashlib.sha256(TypeAdapter(tuple[Candidate, ...]).dump_json(candidates)).hexdigest()


def consolidate(
    candidates: list[Candidate],
    vehicles: set[UUID],
    mandatory: set[UUID],
    priorities: dict[UUID, int],
    *,
    policy_version: str,
    seconds: float,
    threads: int = 1,
    facility_limits: tuple[FacilityLimit, ...] = (),
    external_visits: tuple[tuple[UUID, int, int], ...] = (),
) -> Selection:
    if importlib.metadata.version("ortools") != ORTOOLS_VERSION:
        raise RoutingAdapterError("SOLVER_VERSION_MISMATCH")
    from ortools.sat.python import cp_model

    if seconds <= 0 or not 1 <= threads <= 16:
        raise ValueError("CONSOLIDATION_BUDGET")
    started = time.monotonic()
    # Preserve construction lineage while deduplicating identical candidate identities.
    unique: dict[UUID, Candidate] = {}
    for c in candidates:
        key = c.candidate_id
        if key not in unique or c.cost < unique[key].cost:
            unique[key] = c
    pool = tuple(sorted(unique.values(), key=lambda c: str(c.candidate_id)))
    model = cp_model.CpModel()
    x = [model.new_bool_var(str(c.candidate_id)) for c in pool]
    for vehicle in vehicles:
        model.add(sum(x[i] for i, c in enumerate(pool) if c.vehicle_id == vehicle) == 1)
    for rider in {c.rider_id for c in pool}:
        model.add(sum(x[i] for i, c in enumerate(pool) if c.rider_id == rider) <= 1)
    for lot in {lot for c in pool for lot in c.lot_ids} | mandatory:
        claims = sum(x[i] for i, c in enumerate(pool) if lot in c.lot_ids)
        model.add(claims == 1 if lot in mandatory else claims <= 1)
    for limit in facility_limits:
        external = [
            (a, b) for point, a, b in external_visits if point == limit.receiving_point_id and b > a
        ]
        visits = [
            (i, a, b)
            for i, c in enumerate(pool)
            for point, a, b in c.facility_visits
            if point == limit.receiving_point_id and b > a
        ]
        # Count fixed service intervals, including concurrent visits within one route.
        for instant in sorted({a for _, a, _ in visits} | {a for a, _ in external}):
            model.add(
                sum(x[i] for i, a, b in visits if a <= instant < b)
                + sum(a <= instant < b for a, b in external)
                <= limit.concurrent_unloads
            )
    terms = [
        sum(
            sum(priorities.get(lot) == tier for lot in c.pickup_ids) * x[i]
            for i, c in enumerate(pool)
        )
        for tier in (2, 1)
    ]
    if any(priority == 3 for priority in priorities.values()):
        terms.insert(
            0,
            sum(
                sum(priorities.get(lot) == 3 for lot in c.pickup_ids) * x[i]
                for i, c in enumerate(pool)
            ),
        )
    terms.append(
        sum(sum(lot in priorities for lot in c.pickup_ids) * x[i] for i, c in enumerate(pool))
    )
    terms.append(-sum(c.cost * x[i] for i, c in enumerate(pool)))
    if any(c.supplementary_pickups for c in pool):
        terms.insert(-1, -sum(c.supplementary_pickups * x[i] for i, c in enumerate(pool)))
    statuses, values, bounds = [], [], []
    selected: tuple[UUID, ...] = ()
    for expression in terms:
        remaining = seconds - (time.monotonic() - started)
        if remaining <= 0:
            break
        model.maximize(expression)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = remaining
        solver.parameters.num_search_workers = threads
        solver.parameters.random_seed = 1729
        status = solver.solve(model)
        statuses.append(solver.status_name(status))
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):  # type: ignore[comparison-overlap]
            break
        selected = tuple(c.candidate_id for i, c in enumerate(pool) if solver.value(x[i]))
        value = int(solver.value(expression))
        values.append(value)
        bounds.append(solver.best_objective_bound)
        # A timeout is not permission to sacrifice a higher-priority achieved objective.
        if status != cp_model.OPTIMAL:  # type: ignore[comparison-overlap]
            break
        model.add(expression == value)
        for i in range(len(pool)):
            model.add_hint(x[i], solver.value(x[i]))
        # Hints must be unique across sequential objective stages.
        if len(statuses) < len(terms):
            model.clear_hints()  # type: ignore[no-untyped-call]
    return Selection(
        policy_version=policy_version,
        candidates=pool,
        selected_ids=selected,
        stage_statuses=tuple(statuses),
        stage_values=tuple(values),
        stage_bounds=tuple(bounds),
        elapsed_seconds=time.monotonic() - started,
        pool_digest=pool_digest(pool),
    )


def selected_routes(selection: Selection) -> tuple[ProposedRoute, ...]:
    if selection.version != VERSION or pool_digest(selection.candidates) != selection.pool_digest:
        raise ValueError("SELECTION_PROVENANCE_MISMATCH")
    ids = set(selection.selected_ids)
    if len(ids) != len(selection.selected_ids) or ids - {
        c.candidate_id for c in selection.candidates
    }:
        raise ValueError("SELECTION_MEMBERSHIP")
    if not selection.stage_statuses or selection.stage_statuses[0] not in ("OPTIMAL", "FEASIBLE"):
        raise ValueError("NO_FEASIBLE_SELECTION")
    return tuple(
        ProposedRoute(c.vehicle_id, c.stop_ids)
        for c in selection.candidates
        if c.candidate_id in ids
    )


def verify_selection(
    problem: RoutingProblem,
    selection: Selection,
    economics: EconomicPolicy,
    incumbent: dict[UUID, tuple[UUID, ...]],
    history: tuple[tuple[RoutingProblem, Selection], ...] = (),
) -> None:
    routes = selected_routes(selection)
    if {route.vehicle_id for route in routes} != {v.vehicle_id for v in problem.vehicles}:
        raise ValueError("SELECTION_RESOURCE_COVERAGE")
    pool = {c.candidate_id: c for c in selection.candidates}
    for identity in selection.selected_ids:
        c = pool[identity]
        if c.rebase_source_id is not None:
            origin = next(
                (
                    (p, old)
                    for p, old in history
                    if p.problem_id == c.rebase_source_id and p.snapshot_id == c.rebase_snapshot_id
                ),
                None,
            )
            if origin is None:
                raise ValueError("CANDIDATE_REBASE_SOURCE")
            _, old = origin
            selected_routes(old)
            source = next(
                (
                    item
                    for item in old.candidates
                    if item.candidate_id == c.parent_id and item.candidate_id in old.selected_ids
                ),
                None,
            )
            if source is None or source.vehicle_id != c.vehicle_id:
                raise ValueError("CANDIDATE_REBASE_LINEAGE")
            available = {s.stop_id for s in problem.stops}
            if c.stop_ids != tuple(stop for stop in source.stop_ids if stop in available):
                raise ValueError("CANDIDATE_REBASE_SEQUENCE")
            old_pool = {item.candidate_id: item for item in old.candidates}
            original = source
            seen_origins = set()
            while original.construction == "CONTESTED_REMOVAL":
                if original.candidate_id in seen_origins or original.parent_id not in old_pool:
                    raise ValueError("CANDIDATE_LINEAGE")
                seen_origins.add(original.candidate_id)
                original = old_pool[original.parent_id]
            if (c.construction, c.engine_version) != (
                original.construction,
                original.engine_version,
            ):
                raise ValueError("CANDIDATE_REBASE_ENGINE")
        root = c
        seen = set()
        while root.construction == "CONTESTED_REMOVAL":
            if root.candidate_id in seen or root.parent_id not in pool:
                raise ValueError("CANDIDATE_LINEAGE")
            seen.add(root.candidate_id)
            root = pool[root.parent_id]
        versions = {"VROOM": VROOM_VERSION, "OR_TOOLS": ORTOOLS_VERSION, "INCUMBENT": VERSION}
        if root.construction not in versions or root.engine_version != versions[root.construction]:
            raise ValueError("CANDIDATE_ENGINE_VERSION")
        rebuilt = candidate(
            problem,
            ProposedRoute(c.vehicle_id, c.stop_ids),
            c.construction,
            c.engine_version,
            economics,
            incumbent,
            c.parent_id,
        )
        if (
            rebuilt.rider_id,
            rebuilt.lot_ids,
            rebuilt.pickup_ids,
            rebuilt.cost,
            rebuilt.supplementary_pickups,
            rebuilt.facility_visits,
        ) != (
            c.rider_id,
            c.lot_ids,
            c.pickup_ids,
            c.cost,
            c.supplementary_pickups,
            c.facility_visits,
        ):
            raise ValueError("CANDIDATE_FACTS_CHANGED")
