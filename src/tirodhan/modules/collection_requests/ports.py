from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DeclaredRequestItem:
    item_category_code: str
    declared_quantity: int | None = None
    declared_weight_grams: int | None = None


@dataclass(frozen=True, slots=True)
class QuotedRequestItem:
    quoted_line_amount_minor: int
    pricing_rule_version: str | None = None


@dataclass(frozen=True, slots=True)
class PricingQuote:
    total_amount_minor: int
    currency: str
    items: tuple[QuotedRequestItem, ...]


class PricingPort(Protocol):
    async def quote(self, items: Sequence[DeclaredRequestItem]) -> PricingQuote: ...


class PricingNotConfiguredError(RuntimeError):
    pass


class UnconfiguredPricingPort:
    async def quote(self, items: Sequence[DeclaredRequestItem]) -> PricingQuote:
        raise PricingNotConfiguredError(
            "production pricing is not configured; no fallback quote is available"
        )
