"""Adjacent single-owner resource races followed by real region-scoped planning."""

import asyncio
import json
import os
from pathlib import Path

import h3
import httpx
import pytest
from geographical_evidence import snapshot
from geographical_fixture import DATA, arrivals, operational, setup_region
from geographical_observers import CYCLE, install, observed_workers
from sqlalchemy import select
from test_geographical_rolling import ENABLED, geographical_requirements  # noqa: F401

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.orchestration_models import OperatingRegion, RegionVehicle
from tirodhan.modules.routing_operations.regional_commands import TransferRegionalCapacity
from tirodhan.modules.routing_operations.regional_matrix import RegionalMatrixCache
from tirodhan.modules.routing_operations.regional_planner import run_region

pytestmark = [
    pytest.mark.integration,
    pytest.mark.routing_engine,
    pytest.mark.asyncio,
    pytest.mark.skipif(not ENABLED, reason="explicit geographical native acceptance opt-in"),
]


async def test_adjacent_region_transfer_race_and_authoritative_planning(
    database_session_factory, monkeypatch
):
    from tirodhan.core.logging import configure_logging

    configure_logging("WARNING")
    factory = database_session_factory
    root = Path(os.environ["TIRODHAN_GEO_ARTIFACTS"]) / "overlap"
    (root / "trace").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TIRODHAN_GEO_TRACE", str(root / "trace"))
    install(monkeypatch)
    groups = {}
    for point in DATA["regions"]["West"]["nodes"]:
        cell = h3.latlng_to_cell(point["latitude"], point["longitude"], 7)
        groups.setdefault(cell, []).append(point)
    a, b = next(
        (a, b) for a in sorted(groups) for b in sorted(groups) if b != a and b in h3.grid_disk(a, 1)
    )

    def repeated(cell):
        return (groups[cell] * (280 // len(groups[cell]) + 1))[:280]

    left = await setup_region(factory, "West", 1, 7200, index=0, public_nodes=repeated(a))
    right = await setup_region(
        factory,
        "West",
        1,
        7200,
        index=1,
        public_nodes=repeated(b),
        extra_receiving_points=(left["point"],),
    )
    async with factory() as session:
        source = await session.get(OperatingRegion, left["region_id"])
        destination = await session.get(OperatingRegion, right["region_id"])
        membership = await session.get(RegionVehicle, left["vehicles"][0])
    command = TransferRegionalCapacity(
        client_command_id=new_uuid7(),
        shift_id=left["shifts"][0],
        from_region_id=left["region_id"],
        to_region_id=right["region_id"],
        expected_from_version=source.version,
        expected_to_version=destination.version,
        expected_membership_version=membership.version,
    )
    results = await asyncio.gather(
        operational(factory, left["manager"], command, left["clock"]),
        operational(
            factory,
            left["manager"],
            command.model_copy(update={"client_command_id": new_uuid7()}),
            left["clock"],
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1, results
    async with factory() as session:
        owner = await session.get(RegionVehicle, left["vehicles"][0])
        assert owner.region_id == right["region_id"] and owner.version == membership.version + 1
    # Build the booking in its actual owning service cell, after the transfer.
    await arrivals(factory, right, 1, 1)
    async with httpx.AsyncClient() as client:
        provider = RegionalMatrixCache(
            OSRMMatrixProvider(
                client=client,
                endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
                graph_version=DATA["source_sha256"],
                profile_version="qa-car",
                max_nodes=512,
                max_snap_distance_m=100,
            ),
            graph=DATA["source_sha256"],
            profile="qa-car",
            block_nodes=128,
            max_pairs=262144,
        )
        native_started = asyncio.Event()

        async def observed_live_workers(tasks, processes):
            native_started.set()
            return await observed_workers(tasks, processes)

        async def invoke(region):
            token = CYCLE.set(f"overlap:{region['region_id']}")
            try:
                return await run_region(
                    factory,
                    region_id=region["region_id"],
                    providers={"qa-car": provider},
                    workers=observed_live_workers,
                    clock=lambda: region["clock"],
                )
            finally:
                CYCLE.reset(token)

        first = asyncio.create_task(invoke(right))
        await asyncio.wait_for(native_started.wait(), timeout=120)
        # A real second regional executor cannot publish while the first owns its lease.
        duplicate = await run_region(
            factory,
            region_id=right["region_id"],
            providers={"qa-car": provider},
            workers=observed_workers,
            clock=lambda: right["clock"],
        )
        assert duplicate is None, "Concurrent same-region executor acquired a live owner"
        plans = await asyncio.gather(invoke(left), first)
    async with factory() as session:
        memberships = list(
            await session.scalars(
                select(RegionVehicle).where(
                    RegionVehicle.vehicle_id.in_(left["vehicles"] + right["vehicles"])
                )
            )
        )
        assert len(memberships) == 2 and {m.region_id for m in memberships} == {right["region_id"]}
    # Query all two vehicles under the destination owner for the cross-region conflict check.
    right["vehicles"] += left["vehicles"]
    view = await snapshot(factory, right)
    riders = [a["rider_id"] for a in view["assignments"]]
    assert len(riders) == len(set(riders))
    (root / "QA_OVERLAP.json").write_text(
        json.dumps(
            dict(
                source=str(left["region_id"]),
                destination=str(right["region_id"]),
                transferred_rider=str(left["riders"][0]),
                transfer_results=[type(r).__name__ for r in results],
                actual_transferred_rider_usage=sum(
                    a["rider_id"] == str(left["riders"][0]) for a in view["assignments"]
                ),
                plans=[dict(status=p.status, failure=p.failure_code) if p else None for p in plans],
                authoritative=view,
            ),
            default=str,
            indent=2,
        )
        + "\n"
    )
    assert plans[0] is not None and plans[0].status == "UNCHANGED"
    assert plans[1] is not None and plans[1].status == "ACTIVATED"
    assert len(view["assignments"]) == 1
