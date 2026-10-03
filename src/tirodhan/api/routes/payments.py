from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import (
    get_current_customer_id,
    get_payment_provider,
    get_session_factory,
)
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.customers.service import IdempotencyCommandInProgressError
from tirodhan.modules.payments.checkout import confirm_checkout
from tirodhan.modules.payments.models import PaymentAttempt
from tirodhan.modules.payments.ports import (
    CheckoutConfirmationError,
    CheckoutConfirmationVerifier,
    PaymentProvider,
    PaymentProviderAuthenticationError,
    PaymentProviderNotConfiguredError,
)
from tirodhan.modules.payments.service import (
    InitiatePaymentAttemptCommand,
    PaymentNotEligibleError,
    initiate_payment_attempt,
    process_authenticated_payment_event,
)
from tirodhan.modules.planning.policy import PlanningConfigurationError
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

router = APIRouter(prefix="/v1/payments", tags=["payments"])


class PaymentAttemptResponse(BaseModel):
    payment_attempt_id: UUID
    status: str
    provider: str
    provider_order_id: str | None
    provider_payment_id: str | None
    failure_code: str | None


class ProviderEventResponse(BaseModel):
    payment_provider_event_id: UUID
    processing_status: str


class CheckoutConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    razorpay_order_id: str = Field(min_length=1, max_length=200)
    razorpay_payment_id: str = Field(min_length=1, max_length=200)
    razorpay_signature: SecretStr


@router.post(
    "/attempts/{payment_attempt_id}/checkout-confirmation", response_model=PaymentAttemptResponse
)
async def post_checkout_confirmation(
    payment_attempt_id: UUID,
    body: CheckoutConfirmationRequest,
    customer_id: Annotated[UUID, Depends(get_current_customer_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
) -> PaymentAttemptResponse:
    if not hasattr(provider, "verify_checkout_signature"):
        raise HTTPException(status_code=503, detail="Checkout verification is not configured")
    try:
        return _attempt_response(
            await confirm_checkout(
                session_factory,
                customer_id=customer_id,
                attempt_id=payment_attempt_id,
                order_id=body.razorpay_order_id,
                payment_id=body.razorpay_payment_id,
                signature=body.razorpay_signature.get_secret_value(),
                verifier=cast(CheckoutConfirmationVerifier, provider),
            )
        )
    except CheckoutConfirmationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _attempt_response(attempt: PaymentAttempt) -> PaymentAttemptResponse:
    return PaymentAttemptResponse(
        payment_attempt_id=attempt.payment_attempt_id,
        status=attempt.status,
        provider=attempt.provider,
        provider_order_id=attempt.provider_order_id,
        provider_payment_id=attempt.provider_payment_id,
        failure_code=attempt.failure_code,
    )


@router.post(
    "/collection-requests/{request_id}/attempts",
    response_model=PaymentAttemptResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_payment_attempt(
    request_id: UUID,
    request: Request,
    customer_id: Annotated[UUID, Depends(get_current_customer_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> PaymentAttemptResponse:
    settings = cast(Settings, request.app.state.settings)
    seconds = settings.command_idempotency_ttl_seconds
    if seconds is None:
        raise HTTPException(
            status_code=503, detail="Command idempotency retention is not configured"
        )
    try:
        return _attempt_response(
            await initiate_payment_attempt(
                session_factory,
                InitiatePaymentAttemptCommand(
                    customer_id=customer_id,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                ),
                provider,
                idempotency_expires_at=utc_now() + timedelta(seconds=seconds),
            )
        )
    except PaymentProviderNotConfiguredError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except PaymentNotEligibleError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (IdempotencyKeyConflictError, IdempotencyCommandInProgressError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/provider/webhook", response_model=ProviderEventResponse)
async def post_provider_webhook(
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
) -> ProviderEventResponse:
    raw_body = await request.body()
    try:
        event = await provider.authenticate_webhook(
            raw_body=raw_body,
            headers=dict(request.headers),
        )
        if event.provider != provider.provider_code:
            raise PaymentProviderAuthenticationError("provider identity mismatch")
    except PaymentProviderAuthenticationError as error:
        raise HTTPException(status_code=401, detail="Invalid provider webhook") from error
    except PaymentProviderNotConfiguredError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    settings = cast(Settings, request.app.state.settings)
    try:
        persisted = await process_authenticated_payment_event(
            session_factory,
            event,
            payload_hash=hashlib.sha256(raw_body).digest(),
            planning_lead_time_minutes=settings.planning_lead_time_minutes,
        )
    except PlanningConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return ProviderEventResponse(
        payment_provider_event_id=persisted.payment_provider_event_id,
        processing_status=persisted.processing_status,
    )
