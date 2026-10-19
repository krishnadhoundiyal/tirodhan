"""QA-only forwarding observers. No solver outputs or production decisions are replaced."""

import asyncio
import gzip
import json
import os
import time
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.planning.routing.validation import validate_proposal

CYCLE = ContextVar("geographical_cycle", default="outside-cycle")


def persist(value, *, kind, identity=None):
    root = Path(os.environ["TIRODHAN_GEO_TRACE"])
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{kind}-{identity or uuid4()}.json"
    path.write_text(json.dumps(value, default=str, indent=2) + "\n")


def observed_task(payload):
    """Spawn entry: call the actual solve_task, observing actual adapter invocations inside it."""
    from tirodhan.modules.routing_operations.parallel import solve_task

    metadata = json.loads(payload)
    observations = []
    originals = {cls: cls.propose for cls in (VroomOptimizer, ORToolsOptimizer)}
    for cls in (VroomOptimizer, ORToolsOptimizer):
        original = cls.propose

        def wrap(method):
            async def propose(self, problem, **kwargs):
                started = time.perf_counter()
                entry = {
                    "engine": metadata["engine"],
                    "status": "RUNNING",
                    "task_id": metadata["task_id"],
                    "problem_id": str(problem.problem_id),
                    "bookings": [str(lot.collection_request_id) for lot in problem.lots],
                    "vehicles": [str(v.vehicle_id) for v in problem.vehicles],
                    "search_seconds": kwargs["search_seconds"],
                }
                try:
                    proposal = await method(self, problem, **kwargs)
                    entry.update(
                        status="SUCCEEDED",
                        proposal=asdict(proposal),
                        assessment=asdict(validate_proposal(problem, proposal)),
                    )
                    return proposal
                except Exception as error:
                    entry.update(
                        status="FAILED", failure=str(error), error_type=type(error).__name__
                    )
                    raise
                finally:
                    entry["elapsed_seconds"] = time.perf_counter() - started
                    observations.append(entry)

            return propose

        cls.propose = wrap(original)
    started = time.perf_counter()
    result = None
    try:
        result = solve_task(payload)
        return result
    finally:
        for cls, original in originals.items():
            cls.propose = original
        persist(
            {
                "kind": "native_worker",
                "task_id": metadata["task_id"],
                "engine": metadata["engine"],
                "observations": observations,
                "result": json.loads(result) if result else None,
                "elapsed_seconds": time.perf_counter() - started,
            },
            kind="worker",
            identity=metadata["task_id"],
        )


async def observed_workers(tasks, processes):
    from tirodhan.modules.routing_operations.parallel import local_workers

    persist(
        {
            "kind": "worker_dispatch",
            "cycle": CYCLE.get(),
            "processes": processes,
            "tasks": [
                {
                    "task_id": str(t.task_id),
                    "engine": t.engine.value,
                    "bookings": [str(lot.collection_request_id) for lot in t.problem.lots],
                    "vehicles": [str(v.vehicle_id) for v in t.problem.vehicles],
                }
                for t in tasks
            ],
        },
        kind="dispatch",
    )
    return await local_workers(tasks, processes)


def observed_consolidate(candidates, vehicles, mandatory, priorities, **kwargs):
    from ortools.sat.python import cp_model

    from tirodhan.modules.routing_operations.parallel import consolidate

    stages = []
    original_solve = cp_model.CpSolver.solve
    # consolidate runs in independent threads. Use a ContextVar rather than process-global
    # stage lists; the wrapper is installed once by install(), never changed per invocation.
    token = STAGES.set(stages)
    entry = {
        "kind": "coordinator",
        "cycle": CYCLE.get(),
        "candidate_count": len(candidates),
        "candidates": [c.model_dump(mode="json") for c in candidates],
        "vehicles": sorted(map(str, vehicles)),
        "riders": sorted({str(c.rider_id) for c in candidates}),
        "mandatory": sorted(map(str, mandatory)),
        "priorities": {str(k): v for k, v in priorities.items()},
        "configuration": kwargs,
        "status": "RUNNING",
    }
    started = time.perf_counter()
    try:
        selection = consolidate(candidates, vehicles, mandatory, priorities, **kwargs)
        entry.update(status="SUCCEEDED", selection=selection.model_dump(mode="json"))
        return selection
    except Exception as error:
        entry.update(status="FAILED", failure=str(error))
        raise
    finally:
        assert cp_model.CpSolver.solve is original_solve
        STAGES.reset(token)
        entry.update(elapsed_seconds=time.perf_counter() - started, stages=stages)
        persist(entry, kind="coordinator")


STAGES = ContextVar("geographical_cp_stages", default=None)


def install(monkeypatch):
    from ortools.sat.python import cp_model

    from tirodhan.modules.routing_operations import parallel, regional_planner

    original = cp_model.CpSolver.solve
    original_fetch = OSRMMatrixProvider._fetch

    async def fetch(self, nodes, *args, **kwargs):
        identity = str(uuid4())
        started = time.perf_counter()
        entry = {
            "kind": "road_table",
            "cycle": CYCLE.get(),
            "graph": self.graph_version,
            "profile": self.profile_version,
            "nodes": [asdict(n) for n in nodes],
            "status": "RUNNING",
        }
        try:
            raw = await original_fetch(self, nodes, *args, **kwargs)
            with gzip.open(
                Path(os.environ["TIRODHAN_GEO_TRACE"]) / f"road-{identity}.json.gz", "wb"
            ) as output:
                output.write(raw)
            entry.update(
                status="RESPONSE_RECEIVED",
                raw_response=f"trace/road-{identity}.json.gz",
            )
            try:
                data = json.loads(raw)
                entry["negative_distances"] = [
                    (i, j, value)
                    for i, row in enumerate(data.get("distances", []))
                    for j, value in enumerate(row)
                    if type(value) in (int, float) and value < 0
                ]
            except (ValueError, TypeError, AttributeError):
                # Observation must not replace the provider's own decoding/validation error.
                entry["inspection"] = "UNPARSEABLE_MATRIX_SHAPE"
            return raw
        except asyncio.CancelledError:
            entry.update(status="CANCELLED", error_type="CancelledError")
            raise
        except Exception as error:
            entry.update(status="FAILED", error_type=type(error).__name__)
            raise
        finally:
            entry["elapsed_seconds"] = time.perf_counter() - started
            persist(entry, kind="road", identity=identity)

    monkeypatch.setattr(OSRMMatrixProvider, "_fetch", fetch)

    def solve(self, model, *args, **kwargs):
        stages = STAGES.get()
        started = time.perf_counter()
        # Exact submitted constraints/objective, not a reconstructed description.
        proto = str(model.proto) if stages is not None else None
        status = original(self, model, *args, **kwargs)
        if stages is not None:
            identity = str(uuid4())
            root = Path(os.environ["TIRODHAN_GEO_TRACE"])
            with gzip.open(root / f"cp-model-{identity}.txt.gz", "wt") as output:
                output.write(proto)
            stages.append(
                {
                    "status": self.status_name(status),
                    "elapsed_seconds": time.perf_counter() - started,
                    "objective": self.objective_value,
                    "bound": self.best_objective_bound,
                    "constraint_count": len(model.proto.constraints),
                    "submitted_model": f"trace/cp-model-{identity}.txt.gz",
                }
            )
        return status

    monkeypatch.setattr(cp_model.CpSolver, "solve", solve)
    monkeypatch.setattr(parallel, "solve_task", observed_task)
    monkeypatch.setattr(regional_planner, "consolidate", observed_consolidate)
