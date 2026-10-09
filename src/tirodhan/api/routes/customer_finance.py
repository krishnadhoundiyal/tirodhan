from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.api.routes.customer_reads import Customer, ReadSession
from tirodhan.db.values import utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customer_reads.finance import load_financials
from tirodhan.modules.customer_reads.projections import payment_projection, refund_projection
from tirodhan.modules.customer_reads.schemas import PaymentDto, RefundDto
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.planning.policy import PlanningConfigurationError, planning_cutoff_reached

router = APIRouter(prefix="/v1/customer/collection-requests", tags=["customer finances"])


class RefundsResponse(BaseModel):
    refunds: list[RefundDto]


async def owned_request(
    session: AsyncSession, request_id: UUID, customer: UUID
) -> CollectionRequest:
    row = await session.scalar(
        select(CollectionRequest).where(
            CollectionRequest.request_id == request_id, CollectionRequest.customer_id == customer
        )
    )
    if row is None:
        raise CustomerReadError(404, "NOT_ELIGIBLE")
    return row


async def retry_blocked(
    session: AsyncSession, row: CollectionRequest, lead: int | None, now: datetime
) -> bool:
    try:
        if planning_cutoff_reached(row.slot_start, lead, now=now):
            return True
    except PlanningConfigurationError:
        return True
    return (
        await session.scalar(
            select(PlanningBatch.planning_batch_id).where(
                PlanningBatch.cell_id == row.cell_id,
                PlanningBatch.slot_start == row.slot_start,
                PlanningBatch.slot_end == row.slot_end,
            )
        )
        is not None
    )


@router.get("/{request_id}/payment", response_model=PaymentDto)
async def get_payment(
    request_id: UUID, request: Request, customer: Customer, session: ReadSession
) -> PaymentDto:
    row = await owned_request(session, request_id, customer)
    finance = (await load_financials(session, [row]))[row.request_id]
    now = utc_now()
    return payment_projection(
        row,
        finance.payment,
        finance.attempts,
        now=now,
        reconciliation=finance.reconciliation,
        retry_blocked=await retry_blocked(
            session, row, request.app.state.settings.planning_lead_time_minutes, now
        ),
    )


@router.get("/{request_id}/refunds", response_model=RefundsResponse)
async def get_refunds(
    request_id: UUID, customer: Customer, session: ReadSession
) -> RefundsResponse:
    row = await owned_request(session, request_id, customer)
    finance = (await load_financials(session, [row]))[row.request_id]
    return RefundsResponse(refunds=[refund_projection(r) for r in finance.refunds])
