"""Real protected departure, collection, cargo-bearing replan and validated evidenced discharge."""

import json
import os
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from geographical_evidence import Ledger, snapshot
from geographical_execution import progress
from geographical_fixture import DATA, arrivals, operational, setup_region
from geographical_observers import CYCLE, install, observed_workers
from test_geographical_rolling import ENABLED, geographical_requirements  # noqa: F401

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.operational_commands import AcknowledgeRoute
from tirodhan.modules.routing_operations.regional_planner import run_region

pytestmark = [
    pytest.mark.integration,
    pytest.mark.routing_engine,
    pytest.mark.asyncio,
    pytest.mark.skipif(not ENABLED, reason="explicit geographical native acceptance opt-in"),
]


async def test_real_geographical_custody_lifecycle(database_session_factory, monkeypatch):
    from tirodhan.core.logging import configure_logging

    configure_logging("WARNING")
    factory = database_session_factory
    root = Path(os.environ["TIRODHAN_GEO_ARTIFACTS"]) / "lifecycle"
    (root / "trace").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TIRODHAN_GEO_TRACE", str(root / "trace"))
    install(monkeypatch)
    region = await setup_region(factory, "North", 2, 7200)
    ledger = Ledger()
    snapshots = []
    events = []
    async with httpx.AsyncClient() as client:
        matrix = OSRMMatrixProvider(
            client=client,
            endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
            graph_version=DATA["source_sha256"],
            profile_version="qa-car",
            max_nodes=512,
            max_snap_distance_m=100,
        )
        route_id = None
        for cycle in range(1, 4):
            before = await snapshot(factory, region)
            await arrivals(factory, region, 1, cycle)
            token = CYCLE.set(f"lifecycle:R{cycle}")
            try:
                result = await run_region(
                    factory,
                    region_id=region["region_id"],
                    providers={"qa-car": matrix},
                    workers=observed_workers,
                    clock=lambda: region["clock"],
                )
            finally:
                CYCLE.reset(token)
            assert result is not None and result.status == "ACTIVATED"
            after = await snapshot(factory, region)
            diff = ledger.check(before, after)
            snapshots.append(dict(cycle=cycle, before=before, after=after, diff=diff))
            for route in after["routes"]:
                await operational(
                    factory,
                    UUID(route["rider_id"]),
                    AcknowledgeRoute(
                        client_command_id=new_uuid7(), revision_id=UUID(route["revision_id"])
                    ),
                    region["clock"],
                )
            chosen = next(
                (r for r in after["routes"] if r["route_id"] == route_id), after["routes"][0]
            )
            route_id = chosen["route_id"]
            mode = {1: "depart", 2: "collect", 3: "discharge"}[cycle]
            events.extend(await progress(factory, region, chosen, mode))
        assert any(e["persisted_fact"].get("status") == "VALIDATED" for e in events)
        assert any(e["persisted_fact"].get("outcome") == "COLLECTED" for e in events)
    (root / "QA_LIFECYCLE.json").write_text(
        json.dumps(dict(snapshots=snapshots, events=events), default=str, indent=2) + "\n"
    )
