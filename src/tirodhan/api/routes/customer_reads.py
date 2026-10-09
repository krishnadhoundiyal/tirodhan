from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import (
    get_address_protector,
    get_current_customer_id,
    get_session_factory,
)
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.scheduling import (
    SlotAvailabilityPort,
    SlotConflictError,
    evaluate_slot,
    operating_grid,
)
from tirodhan.modules.customer_reads.catalogue import (
    ProductMediaPort,
    load_catalogue,
    project_catalogue,
)
from tirodhan.modules.customer_reads.cursor import CursorCodec
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customer_reads.repository import collection_page, projections, recommendations
from tirodhan.modules.customer_reads.schemas import (
    CatalogueDto,
    CollectionDetailDto,
    CollectionPageDto,
    OfferedSlotDto,
    RecommendationsDto,
    SlotsDto,
)
from tirodhan.modules.customers.ports import AddressProtector
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.planning.policy import latest_due_slot_start
from tirodhan.modules.serviceability.models import ServiceabilityContext

router = APIRouter(prefix="/v1/customer", tags=["customer projections"])


async def read_session(
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    response: Response,
) -> AsyncIterator[AsyncSession]:
    response.headers["Cache-Control"] = "private, no-store"
    async with factory() as session, session.begin():
        # Related finance/lifecycle facts must come from one coherent DB snapshot.
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        yield session


Customer = Annotated[UUID, Depends(get_current_customer_id)]
ReadSession = Annotated[AsyncSession, Depends(read_session)]


@router.get("/collection-catalogue", response_model=CatalogueDto)
async def get_catalogue(
    request: Request,
    response: Response,
    customer: Customer,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> CatalogueDto:
    response.headers["Cache-Control"] = "private, no-store"
    async with factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        data = await load_catalogue(session)
    storage = cast(ProductMediaPort, request.app.state.product_media)
    try:
        result = await project_catalogue(data, storage)
    except ValidationError as error:
        raise CustomerReadError(503, "NOT_ELIGIBLE") from error
    return result


def validate_context(context: ServiceabilityContext | None, now: datetime) -> ServiceabilityContext:
    if context is None:
        raise CustomerReadError(404, "NOT_ELIGIBLE")
    if context.expires_at <= now:
        raise CustomerReadError(409, "SERVICEABILITY_EXPIRED")
    if context.status != "SERVICEABLE":
        code = (
            "SERVICEABILITY_UNSERVICEABLE" if context.status == "UNSERVICEABLE" else "NOT_ELIGIBLE"
        )
        raise CustomerReadError(503 if context.status == "TECHNICAL_FAILURE" else 409, code)
    if context.cell_id is None or context.location is None:
        raise CustomerReadError(409, "NOT_ELIGIBLE")
    return context


@router.get("/pickup-slots", response_model=SlotsDto)
async def get_slots(
    request: Request,
    customer: Customer,
    session: ReadSession,
    serviceability_context_id: UUID,
) -> SlotsDto:
    now = utc_now()
    context = validate_context(
        await session.scalar(
            select(ServiceabilityContext).where(
                ServiceabilityContext.serviceability_context_id == serviceability_context_id,
                ServiceabilityContext.user_id == customer,
            )
        ),
        now,
    )
    assert context.cell_id is not None
    settings = cast(Settings, request.app.state.settings)
    policy = cast(SlotAvailabilityPort, request.app.state.slot_availability)
    dates = await policy.service_dates(session, cell_id=context.cell_id, now=now)
    # Technical response bound only; exceeding it fails rather than silently
    # inventing a booking horizon or truncating an operational policy.
    if len(dates) > 366:
        raise CustomerReadError(503, "NOT_ELIGIBLE")
    slots: list[OfferedSlotDto] = []
    # Validate the existing lead-time configuration even when no candidates remain.
    cutoff_start = latest_due_slot_start(now, settings.planning_lead_time_minutes)
    grids = [
        slot
        for day in sorted(set(dates))
        for slot in operating_grid(day)
        if slot.start > cutoff_start
    ]
    frozen_windows = (
        set(
            (
                await session.execute(
                    select(PlanningBatch.slot_start, PlanningBatch.slot_end).where(
                        PlanningBatch.cell_id == context.cell_id,
                        tuple_(PlanningBatch.slot_start, PlanningBatch.slot_end).in_(
                            [(slot.start, slot.end) for slot in grids]
                        ),
                    )
                )
            ).all()
        )
        if grids
        else set()
    )
    for slot in grids:
        try:
            available = await evaluate_slot(
                session,
                policy,
                cell_id=context.cell_id,
                slot=slot,
                now=now,
                lead_time_minutes=settings.planning_lead_time_minutes,
                frozen=(slot.start, slot.end) in frozen_windows,
            )
        except SlotConflictError:
            continue
        if available is not None:
            slots.append(
                OfferedSlotDto(
                    start=slot.start,
                    end=slot.end,
                    label=slot.label,
                    slot_id=slot.slot_id,
                    availability=available,
                )
            )
    # Snapshot authorization cannot offer a context which expired during evaluation.
    if context.expires_at <= utc_now():
        raise CustomerReadError(409, "SERVICEABILITY_EXPIRED")
    return SlotsDto(
        serviceability_context_id=context.serviceability_context_id,
        expires_at=context.expires_at,
        slots=slots,
    )


@router.get("/collection-requests", response_model=CollectionPageDto)
async def get_collections(
    request: Request,
    customer: Customer,
    session: ReadSession,
    view: Literal["active", "history"],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> CollectionPageDto:
    settings = cast(Settings, request.app.state.settings)
    if settings.customer_cursor_signing_key is None or settings.customer_cursor_ttl_seconds is None:
        raise CustomerReadError(503, "NOT_ELIGIBLE")
    try:
        codec = CursorCodec(settings.customer_cursor_signing_key.get_secret_value().encode())
    except ValueError as error:
        raise CustomerReadError(503, "NOT_ELIGIBLE") from error
    return await collection_page(
        session,
        owner=customer,
        view=view,
        limit=limit,
        token=cursor,
        codec=codec,
        ttl_seconds=settings.customer_cursor_ttl_seconds,
        now=utc_now(),
        lead_time_minutes=settings.planning_lead_time_minutes,
    )


@router.get("/collection-requests/{request_id}", response_model=CollectionDetailDto)
async def get_detail(
    request_id: UUID,
    request: Request,
    customer: Customer,
    session: ReadSession,
    protector: Annotated[AddressProtector, Depends(get_address_protector)],
) -> CollectionDetailDto:
    row = await session.scalar(
        select(CollectionRequest).where(
            CollectionRequest.request_id == request_id,
            CollectionRequest.customer_id == customer,
        )
    )
    if row is None:
        raise CustomerReadError(404, "NOT_ELIGIBLE")
    settings = cast(Settings, request.app.state.settings)
    result = (
        await projections(
            session,
            [row],
            now=utc_now(),
            lead_time_minutes=settings.planning_lead_time_minutes,
            protector=protector,
        )
    )[0]
    assert isinstance(result, CollectionDetailDto)
    return result


@router.get("/recommendations/collection-categories", response_model=RecommendationsDto)
async def get_recommendations(customer: Customer, session: ReadSession) -> RecommendationsDto:
    return await recommendations(session, customer)
