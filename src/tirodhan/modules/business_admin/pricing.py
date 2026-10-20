"""One configured INR charge per pickup, independent of household item composition."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.collection_requests.ports import (
    DeclaredRequestItem,
    PricingNotConfiguredError,
    PricingQuote,
    QuotedRequestItem,
)
from tirodhan.modules.customer_reads.models import CatalogueCategory, CatalogueGroup

from .history import current_version
from .models import PickupPrice


class FlatPickupPricing:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def quote(self, items: Sequence[DeclaredRequestItem]) -> PricingQuote:
        async with self.factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            offered = {
                r.category_code: r
                for r in await session.scalars(
                    select(CatalogueCategory)
                    .join(CatalogueGroup)
                    .where(
                        CatalogueCategory.active,
                        CatalogueGroup.active,
                        CatalogueCategory.category_code.in_([i.item_category_code for i in items]),
                    )
                )
            }
            if any(
                i.item_category_code not in offered
                or (
                    offered[i.item_category_code].quantity_input == "NONE"
                    and i.declared_quantity is not None
                )
                or (
                    offered[i.item_category_code].weight_input == "NONE"
                    and i.declared_weight_grams is not None
                )
                for i in items
            ):
                from tirodhan.modules.collection_requests.service import CollectionRequestInputError

                raise CollectionRequestInputError("CATEGORY_OR_INPUT_NOT_AVAILABLE")
            rows = list(
                await session.scalars(select(PickupPrice).where(PickupPrice.active).limit(2))
            )
            if (
                len(rows) != 1
                or not items
                or rows[0].amount_minor < 100
                or rows[0].currency != "INR"
            ):
                raise PricingNotConfiguredError("ACTIVE_FLAT_PICKUP_PRICE_REQUIRED")
            row = rows[0]
            revision = await current_version(session, "pickup_price", row.price_id)
            if revision < 1:
                raise PricingNotConfiguredError("VERSIONED_PICKUP_PRICE_REQUIRED")
            rule = f"FLAT:{row.price_id}:{revision}"
            # Zero-charge additional lines are valid immutable booking facts. The first
            # declared line carries the one fee; adding items cannot multiply that fee.
            return PricingQuote(
                row.amount_minor,
                "INR",
                tuple(
                    QuotedRequestItem(row.amount_minor if index == 0 else 0, rule)
                    for index, _ in enumerate(items)
                ),
            )
