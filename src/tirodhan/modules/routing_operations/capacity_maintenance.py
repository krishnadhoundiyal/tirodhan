"""Bounded existing-worker maintenance; no new service, broker or dispatch authority."""

from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.planning.routing.contracts import OptimizerEngine, RouteOptimizer

from .capacity_models import RegionalSlotRefresh
from .slot_projection import refresh_slots
from .snapshot import MatrixSource


async def maintain_capacity(
    factory: async_sessionmaker[AsyncSession],
    region_id: UUID,
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    now: datetime,
    recommend: bool = True,
) -> None:
    from .failure_runtime import activate_due, recommend_due

    await activate_due(factory, region_id, providers=providers, optimizers=optimizers, now=now)
    # Bounded drain with atomic claims; unrelated incidents can progress concurrently.
    if recommend:
        await recommend_due(factory, region_id, providers=providers, optimizers=optimizers, now=now)
    async with factory() as session:
        refresh = await session.get(RegionalSlotRefresh, region_id)
    if refresh is None or refresh.due_at <= now:
        try:
            await refresh_slots(factory, region_id, now=now)
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) not in {"40001", "40P01"}:
                raise
            # Invalidation remains due. Advisory work never blocks authoritative admission.
