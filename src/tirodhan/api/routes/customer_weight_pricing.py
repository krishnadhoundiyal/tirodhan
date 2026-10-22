"""Authenticated quotation has no booking, capacity, financial or external side effect."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_current_customer_id, get_pricing_port, get_session_factory
from tirodhan.modules.business_admin.weight_contracts import (
    ValidatedWeightItem,
    WeightItemInput,
    WeightPolicyResponse,
    WeightQuoteConfirmation,
    WeightQuoteResponse,
)
from tirodhan.modules.business_admin.weight_pricing import (
    WeightPricingConflict,
    active_policy,
    declared_volume,
    policy_fields,
)
from tirodhan.modules.collection_requests.ports import (
    DeclaredRequestItem,
    PricingNotConfiguredError,
    PricingPort,
)

router = APIRouter(prefix="/v1/customer", tags=["customer weight pricing"])
Customer = Annotated[UUID, Depends(get_current_customer_id)]
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


class QuoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_id: UUID
    policy_revision: int = Field(strict=True, ge=1)
    items: list[WeightItemInput] = Field(min_length=1)


@router.get("/weight-pricing-policy", response_model=WeightPolicyResponse)
async def get_policy(
    customer: Customer, factory: Factory, response: Response
) -> WeightPolicyResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        async with factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            row, revision = await active_policy(session)
            return WeightPolicyResponse(
                policy_id=row.policy_id, policy_revision=revision, **policy_fields(row).model_dump()
            )
    except PricingNotConfiguredError as error:
        raise HTTPException(503, str(error)) from None


@router.post("/collection-quotes", response_model=WeightQuoteResponse)
async def post_quote(
    body: QuoteInput,
    customer: Customer,
    response: Response,
    pricing: Annotated[PricingPort, Depends(get_pricing_port)],
) -> WeightQuoteResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        quote = await pricing.quote(
            tuple(DeclaredRequestItem(**i.model_dump()) for i in body.items)
        )
        facts = quote.weight_facts
        if facts is None or quote.declared_items is None:
            raise PricingNotConfiguredError("ACTIVE_WEIGHT_PRICING_POLICY_REQUIRED")
        if (body.policy_id, body.policy_revision) != (facts.policy_id, facts.policy_revision):
            raise WeightPricingConflict("WEIGHT_POLICY_CHANGED_REVIEW_DECLARATIONS")
        volume = declared_volume(quote.declared_items)
        return WeightQuoteResponse(
            items=[
                ValidatedWeightItem.model_validate(
                    dict(
                        item_category_code=i.item_category_code,
                        declared_quantity=i.declared_quantity,
                        declared_weight_grams=i.declared_weight_grams,
                        weight_band=i.weight_band,
                        weight_kg=i.weight_kg,
                        declared_volume_ml=i.declared_volume_ml,
                    )
                )
                for i in quote.declared_items
            ],
            total_weight_grams=facts.total_weight_grams,
            total_volume_ml=volume,
            live_customer_holds_available=volume is not None,
            hold_blocker=None if volume is not None else "ITEM_VOLUME_DECLARATIONS_REQUIRED",
            minimum_amount_minor=facts.minimum_amount_minor,
            maximum_amount_minor=facts.maximum_amount_minor,
            total_amount_minor=quote.total_amount_minor,
            currency="INR",
            confirmation=WeightQuoteConfirmation(
                policy_id=facts.policy_id,
                policy_revision=facts.policy_revision,
                quote_fingerprint=facts.quote_fingerprint,
            ),
        )
    except PricingNotConfiguredError as error:
        raise HTTPException(503, str(error)) from None
    except WeightPricingConflict as error:
        raise HTTPException(409, str(error)) from None
