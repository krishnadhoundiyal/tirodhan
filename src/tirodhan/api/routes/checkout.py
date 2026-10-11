"""Owned, side-effect-free native checkout handoff from persisted financial truth."""

import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_session_factory
from tirodhan.api.routes.customer_reads import Customer
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.payments.models import Payment, PaymentAttempt, PaymentProviderEvent
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.planning.policy import planning_cutoff_reached

router = APIRouter(prefix="/v1/customer", tags=["customer checkout"])


async def checkout_session(
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    response: Response,
) -> AsyncIterator[AsyncSession]:
    response.headers["Cache-Control"] = "private, no-store"
    async with factory() as session, session.begin():
        # Handoff needs the newest committed permission at its final joined SELECT.
        # Other customer projections retain their coherent repeatable-read snapshot.
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY"))
        yield session


CheckoutReadSession = Annotated[AsyncSession, Depends(checkout_session)]


class CheckoutParameters(BaseModel):
    request_id: UUID
    payment_attempt_id: UUID
    provider: Literal["RAZORPAY"]
    public_key_id: str
    provider_order_id: str
    merchant_display_name: str
    amount_minor: int = Field(gt=0, le=9007199254740991)
    currency: Literal["INR"]
    expires_at: datetime
    checkout_ready_until: datetime


@router.get("/payment-attempts/{payment_attempt_id}/checkout", response_model=CheckoutParameters)
async def get_checkout(
    payment_attempt_id: UUID, request: Request, customer: Customer, session: CheckoutReadSession
) -> CheckoutParameters:
    row = (
        await session.execute(
            select(PaymentAttempt, Payment, CollectionRequest)
            .join(Payment, Payment.payment_id == PaymentAttempt.payment_id)
            .join(CollectionRequest, CollectionRequest.request_id == Payment.request_id)
            .where(
                PaymentAttempt.payment_attempt_id == payment_attempt_id,
                CollectionRequest.customer_id == customer,
            )
        )
    ).one_or_none()
    if row is None:
        raise CustomerReadError(404, "NOT_ELIGIBLE")
    attempt, payment, collection = row
    settings = cast(Settings, request.app.state.settings)
    provider = request.app.state.payment_provider
    if not isinstance(provider, RazorpayProvider) or not settings.razorpay_merchant_display_name:
        raise CustomerReadError(503, "NOT_ELIGIBLE")
    now = utc_now()
    if (
        attempt.provider_account_key != provider.account_key
        or attempt.checkout_ready_until is None
        or attempt.checkout_ready_until <= now
        or (attempt.claim_until is not None and attempt.claim_until > now)
        or attempt.provider != "RAZORPAY"
        or attempt.status != "PENDING"
        or payment.status != "PENDING"
        or collection.status != "PENDING_PAYMENT"
        or collection.payment_expires_at <= now
    ):
        raise CustomerReadError(409, "NOT_ELIGIBLE")
    if planning_cutoff_reached(collection.slot_start, settings.planning_lead_time_minutes, now=now):
        raise CustomerReadError(409, "PLANNING_CUTOFF_REACHED")
    frozen = await session.scalar(
        select(PlanningBatch.planning_batch_id).where(
            PlanningBatch.cell_id == collection.cell_id,
            PlanningBatch.slot_start == collection.slot_start,
            PlanningBatch.slot_end == collection.slot_end,
        )
    )
    if frozen is not None:
        raise CustomerReadError(409, "PLANNING_STARTED")
    if (
        attempt.provider_order_id is None
        or re.fullmatch(r"order_[A-Za-z0-9]{1,190}", attempt.provider_order_id) is None
        or payment.currency != "INR"
        or not 0 < payment.amount_minor <= 9007199254740991
        or payment.amount_minor != collection.quoted_amount_minor
        or payment.currency != collection.currency
    ):
        raise CustomerReadError(503, "NOT_ELIGIBLE")
    unsafe = await session.scalar(
        select(PaymentProviderEvent.payment_provider_event_id)
        .where(
            PaymentProviderEvent.provider == attempt.provider,
            or_(
                PaymentProviderEvent.payment_attempt_id == attempt.payment_attempt_id,
                PaymentProviderEvent.provider_order_id == attempt.provider_order_id,
            ),
            PaymentProviderEvent.processing_status.in_({"RECONCILIATION_REQUIRED", "UNMATCHED"}),
        )
        .limit(1)
    )
    if unsafe is not None:
        raise CustomerReadError(409, "NOT_ELIGIBLE")
    # Re-read all handoff predicates in one final committed-state snapshot.
    # A webhook/cancellation between the earlier reads must revoke handoff.
    assert settings.planning_lead_time_minutes is not None
    now = utc_now()
    frozen_query = select(PlanningBatch.planning_batch_id).where(
        PlanningBatch.cell_id == CollectionRequest.cell_id,
        PlanningBatch.slot_start == CollectionRequest.slot_start,
        PlanningBatch.slot_end == CollectionRequest.slot_end,
    )
    unsafe_query = select(PaymentProviderEvent.payment_provider_event_id).where(
        PaymentProviderEvent.provider == PaymentAttempt.provider,
        or_(
            PaymentProviderEvent.payment_attempt_id == PaymentAttempt.payment_attempt_id,
            PaymentProviderEvent.provider_order_id == PaymentAttempt.provider_order_id,
        ),
        PaymentProviderEvent.processing_status.in_({"RECONCILIATION_REQUIRED", "UNMATCHED"}),
    )
    final = (
        await session.execute(
            select(PaymentAttempt, Payment, CollectionRequest)
            .join(Payment, Payment.payment_id == PaymentAttempt.payment_id)
            .join(CollectionRequest, CollectionRequest.request_id == Payment.request_id)
            .where(
                PaymentAttempt.payment_attempt_id == payment_attempt_id,
                CollectionRequest.customer_id == customer,
                PaymentAttempt.provider_account_key == provider.account_key,
                PaymentAttempt.status == "PENDING",
                PaymentAttempt.checkout_ready_until > now,
                or_(PaymentAttempt.claim_until.is_(None), PaymentAttempt.claim_until <= now),
                Payment.status == "PENDING",
                Payment.amount_minor == CollectionRequest.quoted_amount_minor,
                Payment.currency == CollectionRequest.currency,
                CollectionRequest.status == "PENDING_PAYMENT",
                CollectionRequest.planning_batch_id.is_(None),
                CollectionRequest.payment_expires_at > now,
                CollectionRequest.slot_start
                > now + timedelta(minutes=settings.planning_lead_time_minutes),
                ~frozen_query.exists(),
                ~unsafe_query.exists(),
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if final is None:
        raise CustomerReadError(409, "NOT_ELIGIBLE")
    attempt, payment, collection = final
    if (
        collection.payment_expires_at <= utc_now()
        or attempt.checkout_ready_until is None
        or attempt.checkout_ready_until <= utc_now()
    ):
        raise CustomerReadError(409, "NOT_ELIGIBLE")
    return CheckoutParameters(
        request_id=collection.request_id,
        payment_attempt_id=attempt.payment_attempt_id,
        provider="RAZORPAY",
        public_key_id=provider.public_key_id,
        provider_order_id=attempt.provider_order_id,
        merchant_display_name=settings.razorpay_merchant_display_name,
        amount_minor=payment.amount_minor,
        currency="INR",
        expires_at=collection.payment_expires_at,
        checkout_ready_until=attempt.checkout_ready_until,
    )
