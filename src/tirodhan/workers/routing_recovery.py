"""Recovery handler hosted by the existing planning worker, with private finite roads."""

import asyncio
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import OptimizerEngine, RouteOptimizer
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.workers.routing_regional import LocalRoadProfile, road_sources


async def record_blocked(
    factory: async_sessionmaker[AsyncSession], identity: UUID, kind: str, code: str
) -> None:
    from tirodhan.modules.routing_operations.capacity_models import RiderReplacement
    from tirodhan.modules.routing_operations.failure_models import RecoveryAssignment

    async with factory() as session, session.begin():
        if kind == "ACTIVATE":
            blocked = await session.get(RecoveryAssignment, identity, with_for_update=True)
            if blocked and blocked.status not in ("ACTIVATED", "SUPERSEDED"):
                blocked.failure_code = code
        else:
            pending = await session.get(RiderReplacement, identity, with_for_update=True)
            if pending and pending.status in ("REQUESTED", "APPROVED"):
                pending.failure_code = code


async def execute(
    factory: async_sessionmaker[AsyncSession], identity: UUID, kind: str, settings: Settings
) -> None:
    if not (
        settings.routing_dispatch_enabled
        and settings.routing_fleet_enabled
        and settings.routing_orchestration_enabled
        and settings.routing_recovery_profiles_path
    ):
        await record_blocked(factory, identity, kind, "RECOVERY_RUNTIME_UNCONFIGURED_OR_DISABLED")
        raise OperationalInputError("RECOVERY_RUNTIME_UNCONFIGURED_OR_DISABLED")
    try:
        profiles = TypeAdapter(list[LocalRoadProfile]).validate_json(
            await asyncio.to_thread(Path(settings.routing_recovery_profiles_path).read_text)
        )
    except (OSError, ValueError):
        await record_blocked(factory, identity, kind, "RECOVERY_ROAD_PROFILES_UNAVAILABLE")
        raise OperationalInputError("RECOVERY_ROAD_PROFILES_UNAVAILABLE") from None
    async with road_sources(profiles) as providers:
        optimizers: dict[OptimizerEngine, RouteOptimizer] = {
            OptimizerEngine.VROOM: VroomOptimizer(),
            OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
        }
        if kind == "RECOMMEND":
            from tirodhan.modules.routing_operations.capacity_models import RiderReplacement
            from tirodhan.modules.routing_operations.failure_runtime import (
                activate_due,
                recommend_one,
            )

            async with factory() as session:
                row = await session.get(RiderReplacement, identity)
            if row:
                await activate_due(
                    factory,
                    row.region_id,
                    providers=providers,
                    optimizers=optimizers,
                    now=utc_now(),
                )

            await recommend_one(
                factory, identity, providers=providers, optimizers=optimizers, now=utc_now()
            )
        elif kind == "ACTIVATE":
            from tirodhan.modules.routing_operations.failure_execution import activate_recovery

            await activate_recovery(factory, identity, providers=providers, optimizers=optimizers)
        else:
            raise ValueError("INVALID_RECOVERY_KIND")
