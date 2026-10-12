"""One explicitly requested shadow run; no scheduler, queue or production registration."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from tirodhan.core.config import Settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import OptimizerEngine
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.models import RoutingShadowRun
from tirodhan.modules.routing_operations.service import process_shadow_run
from tirodhan.modules.routing_operations.snapshot import MatrixSource, ShadowConfiguration


class RoadProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str
    graph_version: str = Field(min_length=1, max_length=100)
    profile_version: str = Field(min_length=1, max_length=100)
    max_snap_distance_m: float = Field(ge=0, allow_inf_nan=False)
    timeout_seconds: float = Field(gt=0, le=60, allow_inf_nan=False)


async def execute(run_id: UUID, profiles: list[RoadProfile], settings: Settings) -> str:
    if not settings.routing_shadow_review_enabled:
        raise OperationalInputError("ROUTING_REVIEW_DISABLED")
    if len({p.profile_version for p in profiles}) != len(profiles):
        raise OperationalInputError("DUPLICATE_PROFILE_CONFIGURATION")
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            run = await session.get(RoutingShadowRun, run_id)
            if run is None:
                raise OperationalInputError("RUN_NOT_FOUND")
            config = ShadowConfiguration.model_validate(run.configuration)
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            providers: dict[str, MatrixSource] = {
                p.profile_version: OSRMMatrixProvider(
                    client=client,
                    endpoint=p.endpoint,
                    graph_version=p.graph_version,
                    profile_version=p.profile_version,
                    max_nodes=config.max_nodes,
                    max_snap_distance_m=p.max_snap_distance_m,
                    timeout_seconds=p.timeout_seconds,
                )
                for p in profiles
            }
            result = await process_shadow_run(
                factory,
                run_id=run_id,
                providers=providers,
                optimizers={
                    OptimizerEngine.VROOM: VroomOptimizer(),
                    OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
                },
            )
            return result.status
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate one V2 routing snapshot without dispatch"
    )
    parser.add_argument("run_id", type=UUID)
    parser.add_argument("profiles", type=Path)
    args = parser.parse_args()
    settings = Settings()
    configure_logging(settings.log_level, settings.log_file_path)
    profiles = TypeAdapter(list[RoadProfile]).validate_json(args.profiles.read_bytes())
    status = asyncio.run(execute(args.run_id, profiles, settings))
    print(status)
    raise SystemExit(0 if status == "SUCCEEDED" else 1)


if __name__ == "__main__":
    main()
