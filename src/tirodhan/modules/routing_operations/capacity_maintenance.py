"""Bounded existing-worker maintenance; no new service, broker or dispatch authority."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.planning.routing.contracts import OptimizerEngine, RouteOptimizer

from .capacity_models import RegionalSlotRefresh, RiderReplacement
from .replacements import recommend_replacement
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
    async with factory() as session:
        replacement = await session.scalar(
            select(RiderReplacement.replacement_id)
            .where(
                RiderReplacement.region_id == region_id,
                (RiderReplacement.status == "REQUESTED")
                | (
                    (RiderReplacement.status == "APPROVED")
                    & (RiderReplacement.failure_code == "RECOMMENDATION_REQUESTED")
                ),
                RiderReplacement.next_attempt_at <= now,
            )
            .order_by(RiderReplacement.requested_at)
            .limit(1)
        )
        refresh = await session.get(RegionalSlotRefresh, region_id)
    if replacement and recommend:
        await recommend_replacement(
            factory, replacement, providers=providers, optimizers=optimizers, now=now
        )
    if refresh is None or refresh.due_at <= now:
        try:
            await refresh_slots(factory, region_id, now=now)
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) not in {"40001", "40P01"}:
                raise
            # Invalidation remains due. Advisory work never blocks authoritative admission.
