"""One declared-weight calculation, exact bounded pricing, and transaction-fenced confirmation."""

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.collection_requests.ports import (
    DeclaredRequestItem,
    PricingNotConfiguredError,
    PricingQuote,
    QuotedRequestItem,
)
from tirodhan.modules.customer_reads.models import CatalogueCategory, CatalogueGroup
from tirodhan.modules.customers.service import command_fingerprint

from .history import current_version
from .models import WeightPricingPolicy
from .weight_contracts import (
    MAX_GRAMS,
    MAX_VOLUME_ML,
    WeightPolicyFields,
    WeightQuoteConfirmation,
    WeightQuoteFacts,
    kilograms_to_grams,
)


class WeightPricingConflict(ValueError):
    pass


async def lock_pricing(session: AsyncSession, *, exclusive: bool = False) -> None:
    # The same transaction lock covers activation of a previously absent policy (phantoms).
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await session.execute(text(f"SELECT {function}(hashtextextended('tirodhan.weight-pricing',0))"))


async def weight_pricing_selected(session: AsyncSession) -> bool:
    # Once adopted, retirement/missing active configuration never restores legacy flat charging.
    return (
        await session.scalar(
            select(WeightPricingPolicy.policy_id).where(WeightPricingPolicy.adopted).limit(1)
        )
        is not None
    )


async def active_policy(session: AsyncSession) -> tuple[WeightPricingPolicy, int]:
    rows = list(
        await session.scalars(
            select(WeightPricingPolicy).where(WeightPricingPolicy.active).limit(2)
        )
    )
    if len(rows) != 1:
        raise PricingNotConfiguredError("ACTIVE_WEIGHT_PRICING_POLICY_REQUIRED")
    row = rows[0]
    # Validate even a directly written configuration; never fall back on invalid policy.
    policy_fields(row)
    revision = await current_version(session, "weight_pricing_policy", row.policy_id)
    if revision < 1:
        raise PricingNotConfiguredError("VERSIONED_WEIGHT_PRICING_POLICY_REQUIRED")
    return row, revision


def policy_fields(row: WeightPricingPolicy) -> WeightPolicyFields:
    try:
        return WeightPolicyFields.model_validate(
            {name: getattr(row, name) for name in WeightPolicyFields.model_fields}
        )
    except ValueError:
        raise PricingNotConfiguredError("INVALID_WEIGHT_PRICING_POLICY") from None


def normalize_items(
    items: Sequence[DeclaredRequestItem], policy: WeightPolicyFields
) -> tuple[tuple[DeclaredRequestItem, ...], int]:
    if not items:
        raise WeightPricingConflict("ITEM_WEIGHT_DECLARATIONS_REQUIRED")
    result = []
    total = 0
    for item in items:
        if item.weight_band == "BELOW_B":
            grams = policy.first_band_upper_grams
        elif item.weight_band == "B_TO_T":
            grams = policy.numerical_threshold_grams
        elif item.weight_band == "ABOVE_T":
            if item.weight_kg is None:
                raise WeightPricingConflict("NUMERICAL_WEIGHT_REQUIRED")
            try:
                grams = kilograms_to_grams(item.weight_kg)
            except ValueError as error:
                raise WeightPricingConflict(str(error)) from None
            if grams <= policy.numerical_threshold_grams:
                raise WeightPricingConflict("NUMERICAL_WEIGHT_MUST_EXCEED_T")
        else:
            raise WeightPricingConflict("WEIGHT_BAND_REQUIRED")
        if item.weight_band != "ABOVE_T" and item.weight_kg is not None:
            raise WeightPricingConflict("FINITE_BAND_DOES_NOT_ACCEPT_NUMERICAL_WEIGHT")
        if item.declared_weight_grams is not None and (
            type(item.declared_weight_grams) is not int or item.declared_weight_grams != grams
        ):
            raise WeightPricingConflict("DECLARED_WEIGHT_MISMATCH")
        if item.declared_quantity is not None and (
            type(item.declared_quantity) is not int or not 0 < item.declared_quantity <= MAX_GRAMS
        ):
            raise WeightPricingConflict("INVALID_QUANTITY")
        # A line declares its entire selected quantity. Never multiply by quantity.
        total += grams
        if total > MAX_GRAMS:
            raise WeightPricingConflict("AGGREGATE_WEIGHT_OUT_OF_RANGE")
        result.append(replace(item, declared_weight_grams=grams))
    return tuple(result), total


def unrounded_price(total: int, policy: WeightPolicyFields) -> Fraction:
    if type(total) is not int or not 0 <= total <= MAX_GRAMS:
        raise WeightPricingConflict("AGGREGATE_WEIGHT_OUT_OF_RANGE")
    t = Fraction(
        max(
            0,
            min(
                total - policy.included_weight_grams,
                policy.saturation_weight_grams - policy.included_weight_grams,
            ),
        ),
        policy.saturation_weight_grams - policy.included_weight_grams,
    )
    a = Fraction(Decimal(policy.curvature))
    return policy.minimum_amount_minor + (
        policy.maximum_amount_minor - policy.minimum_amount_minor
    ) * ((1 + a) * t / (1 + a * t))


def price_minor(total: int, policy: WeightPolicyFields) -> int:
    value = unrounded_price(total, policy)
    # All prices are nonnegative. Ties round up to the next integer minor unit.
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def declaration_fingerprint(items: Sequence[DeclaredRequestItem]) -> str:
    return command_fingerprint(
        {
            "items": [
                dict(
                    item_category_code=i.item_category_code,
                    declared_quantity=i.declared_quantity,
                    declared_weight_grams=i.declared_weight_grams,
                    weight_band=i.weight_band,
                    weight_kg=i.weight_kg,
                    **(
                        {"declared_volume_ml": i.declared_volume_ml}
                        if i.declared_volume_ml is not None
                        else {}
                    ),
                )
                for i in items
            ]
        }
    ).hex()


def declared_volume(items: Sequence[DeclaredRequestItem]) -> int | None:
    """Sum explicit whole-line occupied volumes. Missing declarations never become zero."""
    total = 0
    complete = bool(items)
    for item in items:
        volume = item.declared_volume_ml
        if volume is None:
            complete = False
            continue
        if type(volume) is not int or not 0 < volume <= MAX_VOLUME_ML:
            raise WeightPricingConflict("ITEM_VOLUME_OUT_OF_RANGE")
        total += volume
        if total > MAX_VOLUME_ML:
            raise WeightPricingConflict("AGGREGATE_VOLUME_OUT_OF_RANGE")
    return total if complete else None


async def quote_in_session(
    session: AsyncSession, items: Sequence[DeclaredRequestItem]
) -> PricingQuote:
    row, revision = await active_policy(session)
    normalized, total = normalize_items(items, policy_fields(row))
    declared_volume(normalized)
    offered = {
        c.category_code: c
        for c in await session.scalars(
            select(CatalogueCategory)
            .join(CatalogueGroup)
            .where(
                CatalogueCategory.active,
                CatalogueGroup.active,
                CatalogueCategory.category_code.in_([i.item_category_code for i in normalized]),
            )
        )
    }
    if any(
        i.item_category_code not in offered
        or (
            offered[i.item_category_code].quantity_input == "NONE"
            and i.declared_quantity is not None
        )
        for i in normalized
    ):
        raise WeightPricingConflict("CATEGORY_OR_INPUT_NOT_AVAILABLE")
    amount = price_minor(total, policy_fields(row))
    fingerprint = command_fingerprint(
        {
            "policy_id": row.policy_id,
            "policy_revision": revision,
            "declarations": declaration_fingerprint(normalized),
            "amount_minor": amount,
            "currency": "INR",
        }
    ).hex()
    facts = WeightQuoteFacts(
        row.policy_id,
        revision,
        total,
        row.minimum_amount_minor,
        row.maximum_amount_minor,
        fingerprint,
    )
    rule = f"WEIGHT:{row.policy_id}:{revision}"
    return PricingQuote(
        amount,
        "INR",
        tuple(
            QuotedRequestItem(amount if index == 0 else 0, rule) for index in range(len(normalized))
        ),
        normalized,
        facts,
    )


def confirm_quote(quote: PricingQuote, confirmation: WeightQuoteConfirmation | None) -> None:
    facts = quote.weight_facts
    if (
        facts is None
        or confirmation is None
        or (confirmation.policy_id, confirmation.policy_revision, confirmation.quote_fingerprint)
        != (facts.policy_id, facts.policy_revision, facts.quote_fingerprint)
    ):
        raise WeightPricingConflict("QUOTE_CONFIRMATION_STALE_OR_MISMATCHED")


async def fence_booking_quote(
    session: AsyncSession, quote: PricingQuote, confirmation: WeightQuoteConfirmation | None
) -> None:
    await lock_pricing(session)
    if quote.weight_facts is None:
        if confirmation is not None or await weight_pricing_selected(session):
            raise WeightPricingConflict("WEIGHT_QUOTE_CONFIRMATION_REQUIRED")
        return
    confirm_quote(quote, confirmation)
    assert quote.declared_items is not None
    if (
        any(i.declared_volume_ml is not None for i in quote.declared_items)
        and declared_volume(quote.declared_items) is None
    ):
        raise WeightPricingConflict("ITEM_VOLUME_DECLARATIONS_REQUIRED")
    row, revision = await active_policy(session)
    if (row.policy_id, revision) != (
        quote.weight_facts.policy_id,
        quote.weight_facts.policy_revision,
    ):
        raise WeightPricingConflict("QUOTE_CONFIRMATION_STALE_OR_MISMATCHED")


class ConfiguredPickupPricing:
    """Transition preserves legacy flat history; adoption of weight policies is fail-closed."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def quote(self, items: Sequence[DeclaredRequestItem]) -> PricingQuote:
        async with self.factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            if await weight_pricing_selected(session):
                return await quote_in_session(session, items)
        if any(
            i.weight_band is not None or i.weight_kg is not None or i.declared_volume_ml is not None
            for i in items
        ):
            raise PricingNotConfiguredError("ACTIVE_WEIGHT_PRICING_POLICY_REQUIRED")
        from .pricing import FlatPickupPricing

        return await FlatPickupPricing(self.factory).quote(items)
