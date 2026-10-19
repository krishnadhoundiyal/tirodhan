"""Actual road deadline/snap rejection, durable retry, then native assignment recovery."""

import json
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from geographical_fixture import DATA, arrivals, resource, setup_region
from geographical_observers import CYCLE, install, observed_workers
from sqlalchemy import func, select
from test_geographical_rolling import ENABLED, geographical_requirements  # noqa: F401

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.commands import PositionReport
from tirodhan.modules.routing_operations.dispatch_models import VehicleRoute
from tirodhan.modules.routing_operations.orchestration_models import FleetWakeup
from tirodhan.modules.routing_operations.regional_planner import run_region, scope

pytestmark = [
    pytest.mark.integration,
    pytest.mark.routing_engine,
    pytest.mark.asyncio,
    pytest.mark.skipif(not ENABLED, reason="explicit geographical native acceptance opt-in"),
]


async def test_actual_provider_failure_retains_bookings_and_recovers(
    database_session_factory, monkeypatch
):
    from tirodhan.core.logging import configure_logging

    configure_logging("WARNING")
    factory = database_session_factory
    root = Path(os.environ["TIRODHAN_GEO_ARTIFACTS"]) / "recovery"
    (root / "trace").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TIRODHAN_GEO_TRACE", str(root / "trace"))
    install(monkeypatch)
    good = DATA["regions"]["North"]["nodes"][0]
    bad = max(
        DATA["regions"]["North"]["excluded_graph_snap_vertices"],
        key=lambda n: max(n["table_snap_distances_m"]),
    )
    region = await setup_region(
        factory,
        "North",
        1,
        7200,
        public_nodes=[good] * 279 + [bad],
        facility_node=DATA["regions"]["North"]["nodes"][1],
    )
    await arrivals(factory, region, 1, 1)
    events = []
    async with httpx.AsyncClient() as client:

        def provider(timeout):
            return OSRMMatrixProvider(
                client=client,
                endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
                graph_version=DATA["source_sha256"],
                profile_version="qa-car",
                max_nodes=512,
                max_snap_distance_m=100,
                timeout_seconds=timeout,
            )

        for timeout, code in ((0.000001, "MATRIX_TIMEOUT"), (30, "INVALID_ROAD_MATRIX")):
            token = CYCLE.set(f"recovery:{code}")
            try:
                with pytest.raises(RoutingAdapterError, match=code):
                    await run_region(
                        factory,
                        region_id=region["region_id"],
                        providers={"qa-car": provider(timeout)},
                        workers=observed_workers,
                        clock=lambda: region["clock"],
                    )
            finally:
                CYCLE.reset(token)
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(VehicleRoute)) == 0
                assert {b.status for b in await session.scalars(select(CollectionRequest))} == {
                    "ACCEPTED"
                }
                wake = await session.get(FleetWakeup, scope(region["region_id"]))
                assert wake.generation > wake.acknowledged
                events.append(
                    dict(
                        failure=code,
                        generation=wake.generation,
                        acknowledged=wake.acknowledged,
                        assignments=0,
                        bookings_retained=1,
                        engines={
                            engine: {"status": "NOT_INVOKED", "reason": code}
                            for engine in ("VROOM", "OR_TOOLS", "CP_SAT_COORDINATOR")
                        },
                    )
                )
            region["clock"] += timedelta(seconds=60)
        # Correct the operational observation through its real command. Do not move the
        # accepted household or relax the road guard. Ten minutes allow this synthetic move.
        region["clock"] += timedelta(minutes=10)
        await resource(
            factory,
            region["manager"],
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=region["vehicles"][0],
                longitude=good["longitude"],
                latitude=good["latitude"],
                observed_at=region["clock"],
            ),
            region["clock"],
        )
        token = CYCLE.set("recovery:valid-native")
        try:
            cycle = await run_region(
                factory,
                region_id=region["region_id"],
                providers={"qa-car": provider(30)},
                workers=observed_workers,
                clock=lambda: region["clock"],
            )
        finally:
            CYCLE.reset(token)
        assert cycle is not None and cycle.status == "ACTIVATED"
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(VehicleRoute)) == 1
        events.append(dict(status=cycle.status, cycle_id=str(cycle.cycle_id), assignments=1))
    (root / "QA_RECOVERY.json").write_text(json.dumps(events, indent=2) + "\n")
