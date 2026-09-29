from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import (
    get_current_customer_id,
    get_pricing_port,
    get_session_factory,
)
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.collection_requests.ports import (
    DeclaredRequestItem,
    PricingNotConfiguredError,
    PricingPort,
)
from tirodhan.modules.collection_requests.service import (
    CollectionRequestInputError,
    CollectionRequestResult,
    CreateCollectionRequestCommand,
    ServiceabilityContextIneligibleError,
    create_collection_request,
)
from tirodhan.modules.customers.service import IdempotencyCommandInProgressError
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

router = APIRouter(prefix="/v1/collection-requests", tags=["collection requests"])


class CollectionRequestItemInput(BaseModel):
    item_category_code: str = Field(min_length=1, max_length=100)
    declared_quantity: int | None = Field(default=None, ge=1)
    declared_weight_grams: int | None = Field(default=None, ge=1)


class CollectionRequestCreate(BaseModel):
    client_request_id: UUID
    serviceability_context_id: UUID
    slot_start: datetime
    slot_end: datetime
    items: list[CollectionRequestItemInput] = Field(min_length=1)


class CollectionRequestItemResponse(BaseModel):
    request_item_id: UUID
    item_category_code: str
    declared_quantity: int | None
    declared_weight_grams: int | None
    quoted_line_amount_minor: int
    currency: str
    pricing_rule_version: str | None


class CollectionRequestResponse(BaseModel):
    request_id: UUID
    client_request_id: UUID
    status: str
    quoted_amount_minor: int
    currency: str
    payment_expires_at: datetime
    payment_id: UUID
    items: list[CollectionRequestItemResponse]


def collection_request_response(result: CollectionRequestResult) -> CollectionRequestResponse:
    return CollectionRequestResponse(
        request_id=result.request.request_id,
        client_request_id=result.request.client_request_id,
        status=result.request.status,
        quoted_amount_minor=result.request.quoted_amount_minor,
        currency=result.request.currency,
        payment_expires_at=result.request.payment_expires_at,
        payment_id=result.payment.payment_id,
        items=[
            CollectionRequestItemResponse(
                request_item_id=item.request_item_id,
                item_category_code=item.item_category_code,
                declared_quantity=item.declared_quantity,
                declared_weight_grams=item.declared_weight_grams,
                quoted_line_amount_minor=item.quoted_line_amount_minor,
                currency=item.currency,
                pricing_rule_version=item.pricing_rule_version,
            )
            for item in result.items
        ],
    )


def _expiries(request: Request) -> tuple[datetime, datetime]:
    settings = cast(Settings, request.app.state.settings)
    payment_seconds = settings.pending_payment_lifetime_seconds
    idempotency_seconds = settings.command_idempotency_ttl_seconds
    if payment_seconds is None or idempotency_seconds is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment lifetime and command idempotency retention must be configured",
        )
    now = utc_now()
    return (
        now + timedelta(seconds=payment_seconds),
        now + timedelta(seconds=idempotency_seconds),
    )


@router.post("", response_model=CollectionRequestResponse, status_code=status.HTTP_201_CREATED)
async def post_collection_request(
    body: CollectionRequestCreate,
    request: Request,
    customer_id: Annotated[UUID, Depends(get_current_customer_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    pricing: Annotated[PricingPort, Depends(get_pricing_port)],
) -> CollectionRequestResponse:
    payment_expires_at, idempotency_expires_at = _expiries(request)
    try:
        result = await create_collection_request(
            session_factory,
            CreateCollectionRequestCommand(
                customer_id=customer_id,
                client_request_id=body.client_request_id,
                serviceability_context_id=body.serviceability_context_id,
                slot_start=body.slot_start,
                slot_end=body.slot_end,
                items=tuple(
                    DeclaredRequestItem(
                        item_category_code=item.item_category_code,
                        declared_quantity=item.declared_quantity,
                        declared_weight_grams=item.declared_weight_grams,
                    )
                    for item in body.items
                ),
                payment_expires_at=payment_expires_at,
            ),
            pricing,
            idempotency_expires_at=idempotency_expires_at,
        )
        return collection_request_response(result)
    except PricingNotConfiguredError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ServiceabilityContextIneligibleError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (
        CollectionRequestInputError,
        IdempotencyKeyConflictError,
        IdempotencyCommandInProgressError,
        IntegrityError,
    ) as error:
        raise HTTPException(status_code=409, detail="Collection request conflicts") from error
