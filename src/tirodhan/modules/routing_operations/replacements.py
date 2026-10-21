"""Finite advisory replacement options using existing regional engines and validation.

Rank independently feasible continuations by committed duty and approved regional cost.
Search exhaustion is UNKNOWN. Approval and physical transfer are separate commands.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    RouteOptimizer,
)

from .admission_queries import in_ids
from .capacity_models import RiderReplacement
from .snapshot import MatrixSource


async def recommend_replacement(
    factory: async_sessionmaker[AsyncSession],
    replacement_id: UUID,
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    now: datetime,
    max_wall_seconds: float = 30,
    recommendation_token: UUID | None = None,
) -> bool:
    if recommendation_token is None:
        from .failure_runtime import recommend_one

        return await recommend_one(
            factory,
            replacement_id,
            providers=providers,
            optimizers=optimizers,
            now=now,
            max_wall_seconds=max_wall_seconds,
        )
    from .replacement_options import evaluate_options

    return await evaluate_options(
        factory,
        replacement_id,
        providers=providers,
        optimizers=optimizers,
        now=now,
        max_wall_seconds=max_wall_seconds,
        recommendation_token=recommendation_token,
    )


async def record_failure(
    factory: async_sessionmaker[AsyncSession],
    request: RiderReplacement,
    version: int,
    code: str,
    now: datetime,
) -> None:
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(RiderReplacement)
            .where(RiderReplacement.replacement_id == request.replacement_id)
            .with_for_update()
        )
        if row is not None and row.version == version and row.status in ("REQUESTED", "APPROVED"):
            row.failure_code = code if row.status == "REQUESTED" else "RECOMMENDATION_REQUESTED"
            row.next_attempt_at = now + timedelta(seconds=min(60, 5 * 2 ** min(row.attempt, 4)))
            row.version += 1


async def replacement_targets(
    session: AsyncSession,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Approved uncollected replacements constrain normal regional optimization/activation."""
    from .failure_models import RecoveryAssignment, RecoveryAssignmentItem

    protected = exists().where(
        RecoveryAssignmentItem.replacement_id == RiderReplacement.replacement_id,
        RecoveryAssignmentItem.released_at.is_(None),
        RecoveryAssignment.assignment_id == RecoveryAssignmentItem.assignment_id,
        RecoveryAssignment.status == "ACTIVATED",
    )
    rows = list(
        await session.scalars(
            select(RiderReplacement).where(
                in_ids(RiderReplacement.request_id, [r["request_id"] for r in requests]),
                or_(
                    RiderReplacement.status == "APPROVED",
                    (RiderReplacement.status == "COMPLETED") & protected,
                ),
            )
        )
    )
    target = {r.request_id: r.recommended_shift_id for r in rows}
    for request in requests:
        if request["request_id"] in target and request["pickup_status"] != "COLLECTED":
            request["replacement_shift_id"] = target[request["request_id"]]
    return [
        {"replacement_id": r.replacement_id, "version": r.version, "target": r.recommended_shift_id}
        for r in rows
    ]
