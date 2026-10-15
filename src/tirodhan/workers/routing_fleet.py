"""One finite due-fleet tick. No infrastructure/schedule or production activation."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from tirodhan.core.config import Settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import OptimizerEngine
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.fleet_models import FleetControl
from tirodhan.modules.routing_operations.matrix_cache import JobMatrixCache
from tirodhan.modules.routing_operations.rolling import SCOPE, FleetPolicy, run_fleet_cycle
from tirodhan.modules.routing_operations.snapshot import MatrixSource
from tirodhan.workers.routing_shadow import RoadProfile


async def execute(profiles: list[RoadProfile], settings: Settings) -> str:
    if not settings.routing_fleet_enabled or not settings.routing_dispatch_enabled:
        raise OperationalInputError("FLEET_OPERATION_DISABLED")
    if len({p.profile_version for p in profiles}) != len(profiles):
        raise OperationalInputError("DUPLICATE_PROFILE_CONFIGURATION")
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            control = await session.get(FleetControl, SCOPE)
            if control is None:
                raise OperationalInputError("FLEET_POLICY_REQUIRED")
            policy = FleetPolicy.model_validate(control.configuration)
        if policy.regional and not settings.routing_orchestration_enabled:
            raise OperationalInputError("REGIONAL_ORCHESTRATION_DISABLED")
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            providers: dict[str, MatrixSource] = {
                p.profile_version: JobMatrixCache(
                    OSRMMatrixProvider(
                        client=client,
                        endpoint=p.endpoint,
                        graph_version=p.graph_version,
                        profile_version=p.profile_version,
                        max_nodes=policy.max_nodes,
                        max_snap_distance_m=p.max_snap_distance_m,
                        timeout_seconds=p.timeout_seconds,
                        max_response_bytes=min(
                            16_000_000, 24 * policy.max_nodes**2 + 1000 * policy.max_nodes
                        ),
                    )
                )
                for p in profiles
            }
            if policy.regional:
                from tirodhan.db.values import utc_now
                from tirodhan.modules.routing_operations.destinations import recover_destinations

                await recover_destinations(
                    factory,
                    providers=providers,
                    optimizers={
                        OptimizerEngine.VROOM: VroomOptimizer(),
                        OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
                    },
                    now=utc_now(),
                )
            cycle = await run_fleet_cycle(
                factory,
                providers=providers,
                optimizers={
                    OptimizerEngine.VROOM: VroomOptimizer(),
                    OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
                },
            )
            if policy.regional:
                from tirodhan.modules.routing_operations.admission_jobs import run_admissions

                await run_admissions(
                    factory,
                    providers=providers,
                    optimizers={
                        OptimizerEngine.VROOM: VroomOptimizer(),
                        OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
                    },
                    lease_seconds=policy.lease_seconds,
                )
            return cycle.status if cycle else "NOT_DUE_OR_OWNED"
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one finite committed-fleet optimization tick")
    parser.add_argument("profiles", type=Path)
    args = parser.parse_args()
    settings = Settings()
    configure_logging(settings.log_level, settings.log_file_path)
    profiles = TypeAdapter(list[RoadProfile]).validate_json(args.profiles.read_bytes())
    status = asyncio.run(execute(profiles, settings))
    print(status)
    raise SystemExit(0 if status in {"ACTIVATED", "UNCHANGED", "NOT_DUE_OR_OWNED"} else 1)


if __name__ == "__main__":
    main()
