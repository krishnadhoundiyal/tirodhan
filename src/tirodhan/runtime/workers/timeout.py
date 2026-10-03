import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.dispatch.models import AssignmentOffer, RiderAssignment
from tirodhan.modules.dispatch.service import ASSIGNMENT_ACTIVE, OFFER_OPEN, _lock_group
from tirodhan.modules.reliability.primitives import append_outbox_event

logger = logging.getLogger(__name__)


async def scan_fleet_timeouts(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 100,
) -> int:
    now = utc_now()
    enqueued_count = 0

    async with session_factory() as session, session.begin():
        assigned_groups_sq = select(RiderAssignment.collection_group_id).where(
            RiderAssignment.status == ASSIGNMENT_ACTIVE
        )

        ind_offers_sq = select(AssignmentOffer.collection_group_id).where(
            AssignmentOffer.audience_kind == "INDEPENDENT"
        )

        stmt = (
            select(AssignmentOffer.collection_group_id)
            .where(
                AssignmentOffer.audience_kind == "FLEET",
                AssignmentOffer.expires_at <= now,
                AssignmentOffer.status == OFFER_OPEN,
                AssignmentOffer.collection_group_id.notin_(assigned_groups_sq),
                AssignmentOffer.collection_group_id.notin_(ind_offers_sq),
            )
            .distinct()
            .limit(batch_size)
        )

        candidates = list(await session.scalars(stmt))

    for group_id in candidates:
        async with session_factory() as session, session.begin():
            try:
                await _lock_group(session, group_id)
            except Exception:
                continue

            assigned = await session.scalar(
                select(RiderAssignment.assignment_id)
                .where(
                    RiderAssignment.collection_group_id == group_id,
                    RiderAssignment.status == ASSIGNMENT_ACTIVE,
                )
                .limit(1)
            )
            if assigned:
                continue

            ind_exists = await session.scalar(
                select(AssignmentOffer.offer_id)
                .where(
                    AssignmentOffer.collection_group_id == group_id,
                    AssignmentOffer.audience_kind == "INDEPENDENT",
                )
                .limit(1)
            )
            if ind_exists:
                continue

            unexpired_fleet = await session.scalar(
                select(AssignmentOffer.offer_id)
                .where(
                    AssignmentOffer.collection_group_id == group_id,
                    AssignmentOffer.audience_kind == "FLEET",
                    AssignmentOffer.expires_at > utc_now(),
                    AssignmentOffer.status == OFFER_OPEN,
                )
                .limit(1)
            )
            if unexpired_fleet:
                continue

            from tirodhan.modules.collection_requests.models import CollectionRequest
            from tirodhan.modules.planning.models import CollectionGroupMember

            req = await session.scalar(
                select(CollectionRequest)
                .join(
                    CollectionGroupMember,
                    CollectionGroupMember.request_id == CollectionRequest.request_id,
                )
                .where(CollectionGroupMember.collection_group_id == group_id)
                .limit(1)
            )
            if not req:
                continue

            await append_outbox_event(
                session,
                event_key=f"collection-group-dispatch-requested:{group_id}:INDEPENDENT",
                aggregate_type="collection_group",
                aggregate_id=group_id,
                event_type="CollectionGroupDispatchRequested",
                payload={
                    "collection_group_id": str(group_id),
                    "planning_batch_id": str(req.planning_batch_id),
                    "cell_id": req.cell_id,
                    "slot_start": req.slot_start.isoformat(),
                    "slot_end": req.slot_end.isoformat(),
                    "dispatch_stage": "INDEPENDENT",
                },
            )
            enqueued_count += 1

    return enqueued_count
