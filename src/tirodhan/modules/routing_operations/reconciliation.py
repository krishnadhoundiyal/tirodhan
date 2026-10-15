"""Batched consequences of final cancellation; never compensates customer state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAssignmentItem
from tirodhan.modules.planning.models import PickupExecution

from .authority import lock_booking_population, lock_resources
from .dispatch_models import RouteTask, VehicleRoute
from .execution import finish_if_empty
from .orchestration import wake


async def reconcile_cancellations(
    factory: async_sessionmaker[AsyncSession], now: datetime, limit: int = 128
) -> int:
    async with factory() as session, session.begin():
        requests = list(
            await session.scalars(
                select(CollectionRequest.request_id)
                .join(PickupExecution)
                .join(RiderAssignmentItem)
                .where(
                    CollectionRequest.status == "CANCELLED",
                    RiderAssignmentItem.released_at.is_(None),
                )
                .order_by(CollectionRequest.request_id)
                .limit(limit)
            )
        )
        if not requests:
            return 0
        await lock_booking_population(session, requests)
        from .models import OperatingShift

        roots = list(
            await session.scalars(
                select(VehicleRoute)
                .join(
                    RiderAssignmentItem,
                    RiderAssignmentItem.assignment_id == VehicleRoute.assignment_id,
                )
                .join(PickupExecution)
                .where(
                    PickupExecution.request_id.in_(requests),
                    RiderAssignmentItem.released_at.is_(None),
                    VehicleRoute.status != "CLOSED",
                )
                .distinct()
                .order_by(VehicleRoute.vehicle_id)
            )
        )
        shifts = list(
            await session.scalars(
                select(OperatingShift).where(
                    OperatingShift.shift_id.in_([r.shift_id for r in roots])
                )
            )
        )
        await lock_resources(
            session,
            sorted(s.rider_id for s in shifts),
            sorted(r.vehicle_id for r in roots),
            authorize=False,
        )
        count = 0
        for task in await session.scalars(
            select(RouteTask)
            .join(PickupExecution)
            .where(
                PickupExecution.request_id.in_(requests),
                RouteTask.state.in_(("PENDING", "STARTED")),
            )
        ):
            pickup = await session.get(PickupExecution, task.pickup_execution_id)
            assert pickup is not None
            if pickup.status == "COLLECTED":
                raise RuntimeError("cancelled booking has collected cargo")
            task.state = "CANCELLED"
        for item in await session.scalars(
            select(RiderAssignmentItem)
            .join(PickupExecution)
            .where(
                PickupExecution.request_id.in_(requests), RiderAssignmentItem.released_at.is_(None)
            )
        ):
            item.released_at, item.release_reason_code = now, "CANCELLED"
            count += 1
        await session.flush()
        for root in roots:
            # Unaffected tasks continue. Their order is preserved until a validated revision.
            await finish_if_empty(session, root, now)
        await wake(session, "CANCELLATIONS_RECONCILED", now)
        return count
