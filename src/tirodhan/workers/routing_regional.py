"""Explicit finite regional invocation; local OSRM children are always supervised."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from tirodhan.core.config import Settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.planning.routing.contracts import RoadMatrix
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider, RoadNode
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.regional_matrix import RegionalMatrixCache
from tirodhan.modules.routing_operations.regional_planner import (
    RegionalPolicy,
    configure_region,
    run_region,
)
from tirodhan.modules.routing_operations.snapshot import MatrixSource


class LazyMatrixSource:
    def __init__(self, source: MatrixSource, start: Callable[[], None]):
        self.source, self.start = source, start

    async def acquire(self, nodes: tuple[RoadNode, ...]) -> RoadMatrix:
        self.start()
        return await self.source.acquire(nodes)

    async def acquire_star(self, nodes: tuple[RoadNode, ...]) -> RoadMatrix:
        self.start()
        method = getattr(self.source, "acquire_star", None)
        return await method(nodes) if method else await self.source.acquire(nodes)

    @property
    def cache_namespace(self) -> object:
        return getattr(self.source, "cache_namespace", None)

    @property
    def max_nodes(self) -> int:
        return int(getattr(self.source, "max_nodes", 32))


class LocalRoadProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_version: str
    graph_version: str
    graph_path: Path
    port: int = Field(ge=1024, le=65535)
    threads: int = Field(ge=1, le=8)
    max_snap_distance_m: float = Field(ge=0, allow_inf_nan=False)
    block_nodes: int = Field(ge=2, le=512)
    max_pairs: int = Field(ge=4, le=2000000)
    matrix_strategy: str = "zonal"


@contextlib.contextmanager
def road_processes(
    profiles: list[LocalRoadProfile], *, binary: str = "osrm-routed"
) -> Iterator[None]:
    if len({p.port for p in profiles}) != len(profiles) or len(
        {p.profile_version for p in profiles}
    ) != len(profiles):
        raise OperationalInputError("DUPLICATE_ROAD_PROFILE_OR_PORT")
    children = []
    try:
        version = subprocess.run([binary, "--version"], capture_output=True, timeout=10, check=True)
        if version.stdout.strip() not in (b"v6.0.0", b"6.0.0"):
            raise OperationalInputError("OSRM_VERSION_MISMATCH")
        for p in profiles:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", p.port)) == 0:
                    raise OperationalInputError("LOCAL_OSRM_PORT_IN_USE")
            children.append(
                subprocess.Popen(
                    [
                        binary,
                        "--algorithm",
                        "mld",
                        "--ip",
                        "127.0.0.1",
                        "--port",
                        str(p.port),
                        "-t",
                        str(p.threads),
                        "--max-table-size",
                        str(p.block_nodes),
                        "--verbosity",
                        "WARNING",
                        str(p.graph_path),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        deadline = time.monotonic() + 60
        with httpx.Client(trust_env=False, timeout=1) as client:
            for p, child in zip(profiles, children, strict=True):
                while True:
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise OperationalInputError("LOCAL_OSRM_STARTUP_FAILED")
                    try:
                        # No coordinates in readiness probes, logs or error text.
                        client.get(f"http://127.0.0.1:{p.port}/")
                        break
                    except httpx.RequestError:
                        time.sleep(0.1)
        yield
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


async def execute(
    region_id: UUID,
    profiles: list[LocalRoadProfile],
    settings: Settings,
    *,
    policy: RegionalPolicy | None = None,
    manager_id: UUID | None = None,
) -> str:
    if not (
        settings.routing_dispatch_enabled
        and settings.routing_fleet_enabled
        and settings.routing_orchestration_enabled
    ):
        raise OperationalInputError("REGIONAL_OPERATION_DISABLED")
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        if policy is not None:
            if manager_id is None:
                raise OperationalInputError("MANAGER_CONFIGURATION_IDENTITY_REQUIRED")
            from tirodhan.db.values import utc_now

            await configure_region(
                factory, region_id=region_id, manager_id=manager_id, policy=policy, now=utc_now()
            )
        if len({p.port for p in profiles}) != len(profiles) or len(
            {p.profile_version for p in profiles}
        ) != len(profiles):
            raise OperationalInputError("DUPLICATE_ROAD_PROFILE_OR_PORT")
        with contextlib.ExitStack() as stack:
            started_profiles: set[str] = set()

            def start(p: LocalRoadProfile) -> None:
                if p.profile_version not in started_profiles:
                    stack.enter_context(road_processes([p]))
                    started_profiles.add(p.profile_version)

            def starter(profile: LocalRoadProfile) -> Callable[[], None]:
                def begin() -> None:
                    start(profile)

                return begin

            async with httpx.AsyncClient(trust_env=False) as client:
                providers: dict[str, MatrixSource] = {
                    p.profile_version: LazyMatrixSource(
                        RegionalMatrixCache(
                            OSRMMatrixProvider(
                                client=client,
                                endpoint=f"http://127.0.0.1:{p.port}",
                                graph_version=p.graph_version,
                                profile_version=p.profile_version,
                                max_nodes=p.block_nodes,
                                max_snap_distance_m=p.max_snap_distance_m,
                                max_response_bytes=16_000_000,
                            ),
                            profile=p.profile_version,
                            graph=p.graph_version,
                            block_nodes=p.block_nodes,
                            max_pairs=p.max_pairs,
                            strategy=p.matrix_strategy,
                        ),
                        starter(p),
                    )
                    for p in profiles
                }
                from tirodhan.modules.planning.routing.adapters import (
                    ORToolsOptimizer,
                    VroomOptimizer,
                )
                from tirodhan.modules.planning.routing.contracts import OptimizerEngine
                from tirodhan.modules.routing_operations.capacity_maintenance import (
                    maintain_capacity,
                )

                cycle = await run_region(factory, region_id=region_id, providers=providers)
                await maintain_capacity(
                    factory,
                    region_id,
                    providers=providers,
                    optimizers={
                        OptimizerEngine.VROOM: VroomOptimizer(),
                        OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
                    },
                    now=utc_now(),
                )
                return cycle.status if cycle else "NOT_DUE_OR_OWNED"
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One finite regional planning job with local roads"
    )
    parser.add_argument("region_id", type=UUID)
    parser.add_argument("profiles", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--manager-id", type=UUID)
    args = parser.parse_args()
    try:
        settings = Settings()
        configure_logging(settings.log_level, settings.log_file_path)
        status = asyncio.run(
            execute(
                args.region_id,
                TypeAdapter(list[LocalRoadProfile]).validate_json(args.profiles.read_bytes()),
                settings,
                policy=RegionalPolicy.model_validate_json(args.policy.read_bytes())
                if args.policy
                else None,
                manager_id=args.manager_id,
            )
        )
    except Exception as error:
        # SQL/transport exception strings can contain restricted inputs. The durable
        # cycle records controlled failure codes; the process boundary never dumps them.
        logging.getLogger(__name__).error(
            "regional_execution_failed", extra={"error_type": type(error).__name__}
        )
        print("REGIONAL_EXECUTION_FAILED")
        raise SystemExit(1) from None
    print(status)


if __name__ == "__main__":
    main()
