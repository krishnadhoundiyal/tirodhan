from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import (
    get_current_customer_id,
    get_payment_provider,
    get_session_factory,
)
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.customers.service import IdempotencyCommandInProgressError
from tirodhan.modules.payments.models import PaymentAttempt
from tirodhan.modules.payments.ports import (
    PaymentProvider,
    PaymentProviderAuthenticationError,
    PaymentProviderEventInputError,
    PaymentProviderNotConfiguredError,
)
from tirodhan.modules.payments.service import (
    InitiatePaymentAttemptCommand,
    PaymentNotEligibleError,
    initiate_payment_attempt,
)
from tirodhan.modules.payments.webhook_queue import MAX_WEBHOOK_BYTES, webhook_message
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.reliability.publisher import MessagePublisher

router = APIRouter(prefix="/v1/payments", tags=["payments"])


class PaymentAttemptResponse(BaseModel):
    payment_attempt_id: UUID
    status: str
    provider: str
    provider_order_id: str | None
    provider_payment_id: str | None
    failure_code: str | None


class ProviderEventResponse(BaseModel):
    receipt_id: str
    processing_status: str


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
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
) -> ProviderEventResponse:
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Provider webhook is too large")
        chunks.extend(chunk)
    raw_body = bytes(chunks)
    try:
        event = await provider.authenticate_webhook(
            raw_body=raw_body,
            headers=dict(request.headers),
        )
        if event.provider != provider.provider_code:
            raise PaymentProviderAuthenticationError("provider identity mismatch")
    except PaymentProviderAuthenticationError as error:
        raise HTTPException(status_code=401, detail="Invalid provider webhook") from error
    except PaymentProviderEventInputError as error:
        raise HTTPException(status_code=422, detail="Malformed provider event") from error
    except PaymentProviderNotConfiguredError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    settings = cast(Settings, request.app.state.settings)
    publisher = cast(MessagePublisher, request.app.state.financial_webhook_publisher)
    if not settings.financial_webhook_queue_name:
        raise HTTPException(status_code=503, detail="Financial webhook queue is not configured")
    try:
        message = webhook_message(event, raw_body)
        await asyncio.wait_for(
            publisher.send(settings.financial_webhook_queue_name, message),
            timeout=settings.financial_webhook_send_timeout_seconds or 4,
        )
    except Exception:
        # Ambiguous send is not acknowledged. Provider replay is safe downstream.
        raise HTTPException(
            status_code=503, detail="Financial webhook delivery unavailable"
        ) from None
    return ProviderEventResponse(
        receipt_id=message.message_id,
        processing_status="QUEUED",
    )
