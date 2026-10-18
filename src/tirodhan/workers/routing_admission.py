"""Finite regional commercial preparation; no dispatch authority or infrastructure."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from tirodhan.core.config import Settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import OptimizerEngine, RouteOptimizer
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.admission_jobs import run_admissions
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.snapshot import MatrixSource
from tirodhan.workers.routing_regional import LazyMatrixSource, LocalRoadProfile, road_processes


async def execute(
    region_id: UUID,
    profiles: list[LocalRoadProfile],
    settings: Settings,
    *,
    max_intents: int = 256,
    max_wall_seconds: int = 120,
) -> int:
    if not settings.routing_orchestration_enabled:
        raise OperationalInputError("REGIONAL_ORCHESTRATION_DISABLED")
    if not 1 <= max_intents or not 1 <= max_wall_seconds <= 900:
        raise OperationalInputError("INVALID_ADMISSION_EXECUTION_BUDGET")
    if len({p.port for p in profiles}) != len(profiles) or len(
        {p.profile_version for p in profiles}
    ) != len(profiles):
        raise OperationalInputError("DUPLICATE_ROAD_PROFILE_OR_PORT")
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        with contextlib.ExitStack() as stack:
            started: set[str] = set()

            def starter(profile: LocalRoadProfile) -> Callable[[], None]:
                def start() -> None:
                    if profile.profile_version not in started:
                        stack.enter_context(road_processes([profile]))
                        started.add(profile.profile_version)

                return start

            async with httpx.AsyncClient(trust_env=False) as client:
                providers: dict[str, MatrixSource] = {
                    p.profile_version: LazyMatrixSource(
                        OSRMMatrixProvider(
                            client=client,
                            endpoint=f"http://127.0.0.1:{p.port}",
                            graph_version=p.graph_version,
                            profile_version=p.profile_version,
                            max_nodes=p.block_nodes,
                            max_snap_distance_m=p.max_snap_distance_m,
                            max_response_bytes=16_000_000,
                        ),
                        starter(p),
                    )
                    for p in profiles
                }
                from tirodhan.db.values import utc_now
                from tirodhan.modules.routing_operations.capacity_maintenance import (
                    maintain_capacity,
                )

                optimizers: dict[OptimizerEngine, RouteOptimizer] = {
                    OptimizerEngine.VROOM: VroomOptimizer(),
                    OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
                }
                await maintain_capacity(
                    factory,
                    region_id,
                    providers=providers,
                    optimizers=optimizers,
                    now=utc_now(),
                    recommend=False,
                )
                return await run_admissions(
                    factory,
                    region_id=region_id,
                    providers=providers,
                    optimizers=optimizers,
                    limit=max_intents,
                    max_wall_seconds=max_wall_seconds,
                    lease_seconds=max_wall_seconds + 30,
                )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="One finite regional commercial admission job")
    parser.add_argument("region_id", type=UUID)
    parser.add_argument("profiles", type=Path)
    parser.add_argument("--max-intents", type=int, default=256)
    parser.add_argument("--max-wall-seconds", type=int, default=120)
    args = parser.parse_args()
    settings = Settings()
    configure_logging(settings.log_level, settings.log_file_path)
    try:
        count = asyncio.run(
            execute(
                args.region_id,
                TypeAdapter(list[LocalRoadProfile]).validate_json(args.profiles.read_bytes()),
                settings,
                max_intents=args.max_intents,
                max_wall_seconds=args.max_wall_seconds,
            )
        )
    except Exception as error:
        logging.getLogger(__name__).error(
            "admission_execution_failed", extra={"error_type": type(error).__name__}
        )
        print("ADMISSION_EXECUTION_FAILED")
        raise SystemExit(1) from None
    print(f"ADMISSION_INTENTS_PROCESSED={count}")


if __name__ == "__main__":
    main()
