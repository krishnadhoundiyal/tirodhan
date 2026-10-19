"""Explicit real geographical acceptance. Run via tests/routing/run-geographical.ps1.

Default collection skips this costly suite; explicit opt-in fails, rather than skips,
when required databases, graphs or native dependencies are missing.
"""

import asyncio
import importlib.metadata
import json
import os
import shutil
import subprocess
import time
import traceback
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from geographical_evidence import Ledger, archive_problem, snapshot, write_evidence
from geographical_execution import progress
from geographical_fixture import DATA, arrivals, operational, setup_region
from geographical_observers import CYCLE, install, observed_workers
from sqlalchemy import select

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.fleet_models import FleetCycle
from tirodhan.modules.routing_operations.models import RoutingShadowRun
from tirodhan.modules.routing_operations.operational_commands import AcknowledgeRoute
from tirodhan.modules.routing_operations.regional_matrix import RegionalMatrixCache
from tirodhan.modules.routing_operations.regional_planner import run_region, scope
from tirodhan.modules.routing_operations.service import PROPOSAL_CODEC

ENABLED = os.getenv("TIRODHAN_GEOGRAPHICAL_QA") == "1"
EXTENDED = os.getenv("TIRODHAN_GEOGRAPHICAL_EXTENDED") == "1"
NAMES = ("West", "North", "South", "East")
FLEETS = dict(zip(NAMES, (10, 15, 20, 25), strict=True))
CASES = [("primary", None, 20, 7200)] + (
    [
        (f"{name}-{threshold}-{horizon}", name, threshold, horizon)
        for name in NAMES
        for threshold in (20, 40, 60)
        for horizon in (7200, 10800, 14400)
    ]
    if EXTENDED
    else []
)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.routing_engine,
    pytest.mark.asyncio,
    pytest.mark.skipif(not ENABLED, reason="explicit geographical native acceptance opt-in"),
]


@pytest.fixture(scope="session", autouse=True)
def geographical_requirements():
    if not ENABLED:
        return
    required = ("TIRODHAN_TEST_DATABASE_URL", "TIRODHAN_OSRM_TEST_URL", "TIRODHAN_GEO_ARTIFACTS")
    for key in required:
        if not os.getenv(key):
            pytest.fail(f"Geographical acceptance requires {key}; use run-geographical.ps1")
    for binary, version in (("vroom", "1.15.0"), ("osrm-routed", "6.0.0")):
        if not shutil.which(binary):
            pytest.fail(f"Required real native binary {binary} unavailable")
        actual = subprocess.run([binary, "--version"], capture_output=True, timeout=10, check=True)
        assert version in actual.stdout.decode(), f"Pinned {binary} {version} required"
    assert importlib.metadata.version("ortools") == "9.14.6206"


@pytest.mark.parametrize("case,name,threshold,horizon", CASES, ids=[c[0] for c in CASES])
async def test_geographical_rolling(
    database_session_factory, monkeypatch, case, name, threshold, horizon
):
    from tirodhan.core.logging import configure_logging

    configure_logging("WARNING")
    factory = database_session_factory
    root = Path(os.environ["TIRODHAN_GEO_ARTIFACTS"]) / case
    root.mkdir(parents=True, exist_ok=True)
    (root / "trace").mkdir(exist_ok=True)
    monkeypatch.setenv("TIRODHAN_GEO_TRACE", str(root / "trace"))
    install(monkeypatch)
    evidence = {
        k: []
        for k in ("bookings", "executions", "snapshots", "assignments", "invariants", "metrics")
    }
    evidence["geography"] = DATA
    evidence["scenario"] = dict(
        case=case,
        threshold=threshold,
        horizon_seconds=horizon,
        seed=1729,
        base="eefabf5c2174533653f71bdf7e961a039c46b261",
    )
    regions = []
    try:
        for index, region_name in enumerate([name] if name else NAMES):
            regions.append(
                await setup_region(
                    factory,
                    region_name,
                    FLEETS[region_name] if name is None else {20: 10, 40: 20, 60: 25}[threshold],
                    horizon,
                    index=index,
                )
            )
        ledgers = {r["region_id"]: Ledger() for r in regions}
        import h3

        evidence["operating_regions"] = [
            dict(
                name=r["name"],
                region_id=str(r["region_id"]),
                fleet_size=r["fleet_size"],
                riders=list(map(str, r["riders"])),
                vehicles=list(map(str, r["vehicles"])),
                origins=r["nodes"][-r["fleet_size"] :],
                service_cells=r["service_cells"],
                service_boundaries=[h3.cell_to_boundary(c) for c in r["service_cells"]],
            )
            for r in regions
        ]
        evidence["physical_events"] = []
        async with httpx.AsyncClient() as client:
            provider = OSRMMatrixProvider(
                client=client,
                endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
                graph_version=DATA["source_sha256"],
                profile_version="qa-car",
                max_nodes=512,
                max_snap_distance_m=100,
                timeout_seconds=30,
                max_response_bytes=32_000_000,
            )
            # Real dense regional matrix provider with existing bounded acquisition/cache.
            matrices = RegionalMatrixCache(
                provider,
                profile="qa-car",
                graph=DATA["source_sha256"],
                block_nodes=256,
                max_pairs=1_048_576,
            )
            for cycle in range(1, 5):
                before = {r["region_id"]: await snapshot(factory, r) for r in regions}
                for region in regions:
                    if cycle > 1:
                        region["clock"] += timedelta(minutes=1)
                    evidence["bookings"].extend(await arrivals(factory, region, threshold, cycle))

                async def invoke(region, cycle=cycle, before=before):
                    label = f"{case}:{region['name']}:R{cycle}"
                    token = CYCLE.set(label)
                    started = time.perf_counter()
                    cycle_result, error = None, None
                    try:
                        cycle_result = await run_region(
                            factory,
                            region_id=region["region_id"],
                            providers={"qa-car": matrices},
                            workers=observed_workers,
                            clock=lambda: region["clock"],
                        )
                    except Exception as failure:
                        (root / f"failure-{region['name']}-R{cycle}.txt").write_text(
                            traceback.format_exc()
                        )
                        error = (
                            str(failure)
                            if type(failure).__name__
                            in {
                                "OperationalInputError",
                                "RoutingAdapterError",
                                "RoutingValidationError",
                                "RouteAuthorityError",
                            }
                            else type(failure).__name__
                        )
                    finally:
                        CYCLE.reset(token)
                    async with factory() as session:
                        persisted = await session.scalar(
                            select(FleetCycle)
                            .where(
                                FleetCycle.cycle_id == cycle_result.cycle_id
                                if cycle_result
                                else FleetCycle.scope == scope(region["region_id"])
                            )
                            .order_by(FleetCycle.created_at.desc(), FleetCycle.cycle_id.desc())
                            .limit(1)
                        )
                        if persisted and persisted.run_id:
                            run = await session.get(RoutingShadowRun, persisted.run_id)
                            archive_problem(root, run)
                    after = await snapshot(factory, region)
                    if cycle_result and cycle_result.status == "ACTIVATED":
                        assert persisted.cycle_id == cycle_result.cycle_id
                        published = PROPOSAL_CODEC.validate_python(
                            run.results["COMPOSITE"]["proposal"]
                        )
                        assert {
                            r["vehicle_id"]: tuple(
                                s["stop_id"]
                                for s in r["stops"]
                                if s["state"] in {"PENDING", "STARTED"}
                            )
                            for r in after["routes"]
                            if r["status"] != "CLOSED"
                        } == {
                            str(r.vehicle_id): tuple(map(str, r.stop_ids))
                            for r in published.routes
                            if r.stop_ids
                        }, "Recorded publication does not match committed pending task order"
                    diff, violation = None, None
                    try:
                        diff = ledgers[region["region_id"]].check(
                            before[region["region_id"]], after
                        )
                    except AssertionError as failure:
                        violation = str(failure)
                    result = dict(
                        label=label,
                        region=region["name"],
                        region_id=str(region["region_id"]),
                        cycle=cycle,
                        threshold=threshold,
                        new_bookings=threshold,
                        total_bookings=len(region["bookings"]),
                        previously_assigned=len(before[region["region_id"]]["assignments"]),
                        horizon_seconds=horizon,
                        fleet_size=region["fleet_size"],
                        trigger="QA_THRESHOLD_DRIVER",
                        trigger_reason=f"{threshold} new paid accepted bookings accumulated",
                        invoked_at=region["clock"].isoformat(),
                        executor=scope(region["region_id"]),
                        cycle_id=str(cycle_result.cycle_id)
                        if cycle_result
                        else str(persisted.cycle_id)
                        if persisted
                        else None,
                        run_id=str(persisted.run_id) if persisted and persisted.run_id else None,
                        status=cycle_result.status
                        if cycle_result
                        else "FAILED"
                        if error
                        else "NOT_INVOKED",
                        failure=error or (cycle_result.failure_code if cycle_result else None),
                        elapsed_seconds=time.perf_counter() - started,
                        actual_new_assignments=len((diff or {}).get("added", [])),
                        measured_generation=(persisted.measurements or {}).get("generation")
                        if persisted
                        else None,
                        production_measurements=persisted.measurements if persisted else None,
                    )
                    return result, after, diff, violation

                results = await asyncio.gather(*(invoke(r) for r in regions))
                # Append evidence in stable region order even when execution overlaps.
                for region, (result, after, diff, violation) in zip(regions, results, strict=True):
                    evidence["executions"].append(result)
                    evidence["snapshots"].append(
                        dict(
                            label=result["label"],
                            before=before[region["region_id"]],
                            after=after,
                            diff=diff,
                        )
                    )
                    evidence["invariants"].append(
                        dict(
                            label=result["label"],
                            violation=violation,
                            committed_preserved=not violation,
                            independent_proposal_validation="all persisted current revisions",
                            historical_commitments=len(ledgers[region["region_id"]].committed),
                        )
                    )
                    evidence["assignments"].extend(
                        dict(cycle=cycle, **a) for a in after["assignments"]
                    )
                    evidence["metrics"].append(
                        dict(
                            label=result["label"],
                            status=result["status"],
                            assigned=len(after["assignments"]),
                            used_riders=len({a["rider_id"] for a in after["assignments"]}),
                            declared_fleet=region["fleet_size"],
                            deferred=len(region["bookings"]) - len(after["assignments"]),
                            distance_m=sum(
                                r["distance_m"] for r in after["routes"] if r["status"] != "CLOSED"
                            ),
                            travel_seconds=sum(
                                r["travel_seconds"]
                                for r in after["routes"]
                                if r["status"] != "CLOSED"
                            ),
                        )
                    )
                    for route in after["routes"]:
                        if route["status"] == "ACTIVE":
                            from uuid import UUID

                            await operational(
                                factory,
                                UUID(route["rider_id"]),
                                AcknowledgeRoute(
                                    client_command_id=new_uuid7(),
                                    revision_id=UUID(route["revision_id"]),
                                ),
                                region["clock"],
                            )
                write_evidence(root, evidence)
                if name is None and cycle in {1, 2, 4}:
                    region = next(r for r in regions if r["name"] == "North")
                    view = next(
                        after for result, after, _, _ in results if result["region"] == "North"
                    )
                    active = [r for r in view["routes"] if r["status"] == "ACTIVE"]
                    if active:
                        mode = {1: "depart", 2: "collect", 4: "discharge"}[cycle]
                        chosen = next(
                            (
                                r
                                for r in active
                                if r["route_id"] == region.get("execution_route_id")
                            ),
                            active[0],
                        )
                        region["execution_route_id"] = chosen["route_id"]
                        events = await progress(factory, region, chosen, mode)
                        evidence["physical_events"].extend(
                            dict(region=region["name"], cycle=cycle, **e) for e in events
                        )
        assert all(e["status"] == "ACTIVATED" for e in evidence["executions"]), evidence[
            "executions"
        ]
        assert all(i["violation"] is None for i in evidence["invariants"]), evidence["invariants"]
        assert len(evidence["executions"]) == 4 * len(regions)
        assert all(e["actual_new_assignments"] > 0 for e in evidence["executions"]), (
            "Activation did not incorporate new work"
        )
        traces = json.loads((root / "QA_SOLVER_TRACE.json").read_text())
        for execution in evidence["executions"]:
            dispatches = [
                t
                for t in traces
                if t["kind"] == "worker_dispatch" and t["cycle"] == execution["label"]
            ]
            ids = {task["task_id"] for d in dispatches for task in d["tasks"]}
            for engine in ("VROOM", "OR_TOOLS"):
                assert any(
                    t.get("engine") == engine and t.get("observations") and t.get("task_id") in ids
                    for t in traces
                )
            assert any(
                t["kind"] == "coordinator" and t["cycle"] == execution["label"] and t["stages"]
                for t in traces
            )
    finally:
        write_evidence(root, evidence)
