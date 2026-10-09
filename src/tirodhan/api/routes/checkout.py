"""Owned, side-effect-free native checkout handoff from persisted financial truth."""

import re
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from tirodhan.api.routes.customer_reads import Customer, ReadSession
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.payments.models import Payment, PaymentAttempt
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.planning.policy import planning_cutoff_reached

router = APIRouter(prefix="/v1/customer", tags=["customer checkout"])


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


@router.get("/payment-attempts/{payment_attempt_id}/checkout", response_model=CheckoutParameters)
async def get_checkout(
    payment_attempt_id: UUID, request: Request, customer: Customer, session: ReadSession
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
        attempt.provider != "RAZORPAY"
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
    if collection.payment_expires_at <= utc_now():
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
    )
