from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.collection_requests.models import CollectionRequest, CollectionRequestItem
from tirodhan.modules.collection_requests.scheduling import SlotWindow
from tirodhan.modules.customer_reads.cursor import Cursor, CursorCodec
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customer_reads.finance import load_financials
from tirodhan.modules.customer_reads.models import CatalogueCategory, CatalogueGroup
from tirodhan.modules.customer_reads.projections import (
    cancellation_projection,
    journey_projection,
    payment_projection,
    refund_projection,
)
from tirodhan.modules.customer_reads.schemas import (
    AddressDto,
    CollectionDetailDto,
    CollectionItemDto,
    CollectionPageDto,
    CollectionSummaryDto,
    MoneyDto,
    RecommendationItemDto,
    RecommendationsDto,
    SlotDto,
)
from tirodhan.modules.customers.ports import AddressProtector
from tirodhan.modules.handovers.models import HandoverEvent, HandoverEventItem
from tirodhan.modules.planning.models import PickupExecution, PlanningBatch
from tirodhan.modules.planning.policy import PlanningConfigurationError, planning_cutoff_reached

ACTIVE = ("PENDING_PAYMENT", "ACCEPTED", "PRE_PLANNING", "PLANNED")
HISTORY = ("CANCELLED", "COMPLETED", "EXPIRED")


async def projections(
    session: AsyncSession,
    requests: list[CollectionRequest],
    *,
    now: datetime,
    lead_time_minutes: int | None,
    protector: AddressProtector | None = None,
) -> list[CollectionSummaryDto]:
    if not requests:
        return []
    ids = [r.request_id for r in requests]
    items_by_request: dict[UUID, list[CollectionRequestItem]] = defaultdict(list)
    for item in await session.scalars(
        select(CollectionRequestItem)
        .where(CollectionRequestItem.request_id.in_(ids))
        .order_by(CollectionRequestItem.created_at, CollectionRequestItem.request_item_id)
    ):
        items_by_request[item.request_id].append(item)
    finances = await load_financials(session, requests)
    pickups = list(
        await session.scalars(select(PickupExecution).where(PickupExecution.request_id.in_(ids)))
    )
    pickup_by_request = {p.request_id: p for p in pickups}
    handovers: dict[UUID, list[HandoverEvent]] = defaultdict(list)
    for pickup_id, event in (
        await session.execute(
            select(
                HandoverEventItem.pickup_execution_id,
                HandoverEvent,
            )
            .join(HandoverEventItem)
            .where(
                HandoverEventItem.pickup_execution_id.in_([p.pickup_execution_id for p in pickups]),
                HandoverEventItem.status == HandoverEvent.status,
            )
            .order_by(HandoverEvent.created_at, HandoverEvent.handover_event_id)
        )
    ).all():
        handovers[pickup_id].append(event)
    batches = list(
        await session.scalars(
            select(PlanningBatch).where(
                tuple_(PlanningBatch.cell_id, PlanningBatch.slot_start, PlanningBatch.slot_end).in_(
                    [(r.cell_id, r.slot_start, r.slot_end) for r in requests]
                )
            )
        )
    )
    frozen = {(batch.cell_id, batch.slot_start, batch.slot_end) for batch in batches}
    batch_by_id = {batch.planning_batch_id: batch for batch in batches}
    results: list[CollectionSummaryDto] = []
    for request in requests:
        finance = finances[request.request_id]
        payment = finance.payment
        request_items = items_by_request[request.request_id]
        if not request_items:
            raise CustomerReadError(503, "NOT_ELIGIBLE")
        pickup = pickup_by_request.get(request.request_id)
        events = handovers[pickup.pickup_execution_id] if pickup else []
        journey = journey_projection(request, pickup, events)
        refunded = [refund_projection(r) for r in finance.refunds]
        complete = [m.code for m in journey.milestones if m.state == "COMPLETE"]
        timestamps = [request.created_at]
        batch = batch_by_id.get(request.planning_batch_id) if request.planning_batch_id else None
        timestamps.extend(
            x
            for x in (
                request.accepted_at,
                request.cancelled_at,
                request.completed_at,
                request.expired_at,
                payment.succeeded_at,
                payment.cancelled_at,
                payment.expired_at,
                batch.created_at if batch else None,
                batch.completed_at if batch else None,
                pickup.updated_at if pickup else None,
            )
            if x is not None
        )
        timestamps.extend(e.created_at for e in events)
        timestamps.extend(e.evaluated_at for e in events)
        timestamps.extend(a.completed_at or a.created_at for a in finance.attempts)
        timestamps.extend(
            r.completed_at or r.processing_started_at or r.created_at for r in finance.refunds
        )
        slot = SlotWindow(request.slot_start, request.slot_end)
        summary = CollectionSummaryDto.model_validate(
            {
                "request_id": request.request_id,
                "status": request.status,
                "title": {
                    "PENDING_PAYMENT": "Payment pending",
                    "ACCEPTED": "Collection scheduled",
                    "PRE_PLANNING": "Collection planning",
                    "PLANNED": "Collection planned",
                    "COMPLETED": "Collection completed",
                    "CANCELLED": "Collection cancelled",
                    "EXPIRED": "Collection expired",
                }[request.status],
                "slot": SlotDto(start=slot.start, end=slot.end, label=slot.label),
                # There is no approved locality-only historical field. Never parse a
                # full household address heuristically into a supposedly safe summary.
                "address_summary": "",
                "category_codes": list(
                    dict.fromkeys(item.item_category_code for item in request_items)
                ),
                "image": None,
                "journey_status": complete[-1] if complete else "NOT_STARTED",
                "refund_status": refunded[-1].status if refunded else None,
                "created_at": request.created_at,
                "updated_at": max(timestamps),
            }
        )
        if protector is None:
            results.append(summary)
            continue
        text = await protector.unprotect(bytes(request.pickup_address_snapshot_encrypted))
        try:
            cutoff = planning_cutoff_reached(request.slot_start, lead_time_minutes, now=now)
        except PlanningConfigurationError:
            cutoff = True
        results.append(
            CollectionDetailDto(
                **summary.model_dump(),
                address=AddressDto(label=None, text=text),
                items=[
                    CollectionItemDto(
                        category_code=i.item_category_code,
                        display_name=i.display_name_snapshot or i.item_category_code,
                        declared_quantity=i.declared_quantity,
                        declared_weight_grams=i.declared_weight_grams,
                        quoted_line_amount_minor=i.quoted_line_amount_minor,
                        image=None,
                    )
                    for i in request_items
                ],
                quote=MoneyDto(amount_minor=request.quoted_amount_minor, currency=request.currency),
                payment=payment_projection(
                    request,
                    payment,
                    finance.attempts,
                    now=now,
                    reconciliation=finance.reconciliation,
                    retry_blocked=cutoff
                    or (request.cell_id, request.slot_start, request.slot_end) in frozen,
                ),
                cancellation=cancellation_projection(
                    request,
                    payment,
                    now=now,
                    lead_time_minutes=lead_time_minutes,
                    frozen=(request.cell_id, request.slot_start, request.slot_end) in frozen,
                    refunds=finance.refunds,
                ),
                journey=journey,
                refunds=refunded,
                cancelled_at=request.cancelled_at,
                completed_at=request.completed_at,
            )
        )
    return results


async def collection_page(
    session: AsyncSession,
    *,
    owner: UUID,
    view: Literal["active", "history"],
    limit: int,
    token: str | None,
    codec: CursorCodec,
    ttl_seconds: int,
    now: datetime,
    lead_time_minutes: int | None,
) -> CollectionPageDto:
    cursor = codec.decode(token, owner=owner, view=view, now=now) if token else None
    snapshot = cursor.snapshot if cursor else now
    expires_at = cursor.expires_at if cursor else now + timedelta(seconds=ttl_seconds)
    query = select(CollectionRequest).where(
        CollectionRequest.customer_id == owner,
        CollectionRequest.status.in_(ACTIVE if view == "active" else HISTORY),
        CollectionRequest.created_at <= snapshot,
    )
    if cursor:
        query = query.where(
            tuple_(CollectionRequest.created_at, CollectionRequest.request_id)
            < tuple_(literal(cursor.created_at), literal(cursor.request_id))
        )
    rows = list(
        await session.scalars(
            query.order_by(
                CollectionRequest.created_at.desc(), CollectionRequest.request_id.desc()
            ).limit(limit + 1)
        )
    )
    page = rows[:limit]
    next_cursor = (
        codec.encode(
            Cursor(owner, view, snapshot, expires_at, page[-1].created_at, page[-1].request_id)
        )
        if len(rows) > limit
        else None
    )
    return CollectionPageDto(
        items=await projections(
            session,
            page,
            now=now,
            lead_time_minutes=lead_time_minutes,
        ),
        next_cursor=next_cursor,
    )


async def recommendations(session: AsyncSession, owner: UUID) -> RecommendationsDto:
    # Count distinct completed collections, then recency, then code. Duplicate
    # declaration lines do not inflate frequency; no other customer's data enters.
    rows = await session.scalars(
        select(CollectionRequestItem.item_category_code)
        .join(
            CollectionRequest,
            CollectionRequest.request_id == CollectionRequestItem.request_id,
        )
        .join(
            CatalogueCategory,
            CatalogueCategory.category_code == CollectionRequestItem.item_category_code,
        )
        .join(CatalogueGroup)
        .where(
            CollectionRequest.customer_id == owner,
            CollectionRequest.status == "COMPLETED",
            CatalogueCategory.active.is_(True),
            CatalogueGroup.active.is_(True),
            CatalogueCategory.image_id.is_not(None),
            CatalogueCategory.thumbnail_id.is_not(None),
        )
        .group_by(CollectionRequestItem.item_category_code)
        .order_by(
            func.count(func.distinct(CollectionRequest.request_id)).desc(),
            func.max(CollectionRequest.created_at).desc(),
            CollectionRequestItem.item_category_code,
        )
        .limit(12)
    )
    return RecommendationsDto(
        recommendations=[
            RecommendationItemDto(category_code=code, rank=i) for i, code in enumerate(rows, 1)
        ]
    )
