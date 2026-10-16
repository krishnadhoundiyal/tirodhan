"""Both local graph/profile processes are reusable and terminate with the finite job."""

import os
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider, RoadNode
from tirodhan.workers.routing_regional import LocalRoadProfile, road_processes


@pytest.mark.asyncio
async def test_local_car_and_bicycle_processes_and_cleanup():
    graph = os.getenv("TIRODHAN_REGIONAL_GRAPH_DIR")
    if not graph:
        pytest.skip("explicit local graph runtime opt-in")
    profiles = [
        LocalRoadProfile(
            profile_version=name,
            graph_version=f"fixture-{name}",
            graph_path=Path(graph) / name / "map.osrm",
            port=5021 + i,
            threads=1,
            max_snap_distance_m=300,
            block_nodes=128,
            max_pairs=65536,
        )
        for i, name in enumerate(("car", "bicycle"))
    ]
    nodes = (RoadNode(UUID(int=1), 77.207, 28.632), RoadNode(UUID(int=2), 77.2105, 28.6305))
    with road_processes(profiles):
        async with httpx.AsyncClient(trust_env=False) as client:
            matrices = []
            for p in profiles:
                provider = OSRMMatrixProvider(
                    client=client,
                    endpoint=f"http://127.0.0.1:{p.port}",
                    graph_version=p.graph_version,
                    profile_version=p.profile_version,
                    max_nodes=128,
                    max_snap_distance_m=300,
                )
                matrices.append(await provider.acquire(nodes))
            assert matrices[0].profile_version == "car"
            assert matrices[1].profile_version == "bicycle"
            assert matrices[0].travel_seconds != matrices[1].travel_seconds
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        for p in profiles:
            with pytest.raises(httpx.RequestError):
                await client.get(f"http://127.0.0.1:{p.port}/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disabled",
    ["routing_dispatch_enabled", "routing_fleet_enabled", "routing_orchestration_enabled"],
)
async def test_disabled_entry_never_opens_database_or_native_runtime(monkeypatch, disabled):
    from tirodhan.core.config import Settings
    from tirodhan.modules.routing_operations.commands import OperationalInputError
    from tirodhan.workers import routing_regional

    flags = dict(
        routing_dispatch_enabled=True,
        routing_fleet_enabled=True,
        routing_orchestration_enabled=True,
    )
    flags[disabled] = False
    monkeypatch.setattr(
        routing_regional,
        "create_database_engine",
        lambda _: pytest.fail("disabled entry opened database"),
    )
    with pytest.raises(OperationalInputError, match="REGIONAL_OPERATION_DISABLED"):
        await routing_regional.execute(UUID(int=1), [], Settings(_env_file=None, **flags))


def test_occupied_road_port_is_rejected_without_launching_a_child(monkeypatch):
    import socket
    from types import SimpleNamespace

    from tirodhan.modules.routing_operations.commands import OperationalInputError
    from tirodhan.workers import routing_regional

    monkeypatch.setattr(
        routing_regional.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=b"v6.0.0")
    )
    monkeypatch.setattr(
        routing_regional.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("occupied port launched child"),
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        p = LocalRoadProfile(
            profile_version="car",
            graph_version="fixture",
            graph_path=Path("/tmp/not-opened.osrm"),
            port=listener.getsockname()[1],
            threads=1,
            max_snap_distance_m=300,
            block_nodes=4,
            max_pairs=16,
        )
        with pytest.raises(OperationalInputError, match="LOCAL_OSRM_PORT_IN_USE"):
            with road_processes([p]):
                pytest.fail("occupied road port accepted")


def test_regional_cli_never_renders_raw_failure_inputs(monkeypatch, tmp_path, capsys, caplog):
    from tirodhan.workers import routing_regional

    sensitive = "restricted-input-that-must-not-be-rendered"

    async def failure(*args, **kwargs):
        raise RuntimeError(sensitive)

    profile_file = tmp_path / "profiles.json"
    profile_file.write_text("[]")
    monkeypatch.setattr(routing_regional, "execute", failure)
    monkeypatch.setattr(routing_regional, "configure_logging", lambda *a: None)
    monkeypatch.setattr("sys.argv", ["routing-regional", str(UUID(int=1)), str(profile_file)])
    with pytest.raises(SystemExit) as error:
        routing_regional.main()
    assert error.value.code == 1
    rendered = capsys.readouterr()
    assert rendered.out == "REGIONAL_EXECUTION_FAILED\n"
    assert sensitive not in rendered.out + rendered.err + caplog.text
