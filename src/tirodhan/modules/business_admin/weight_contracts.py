"""Typed weight declarations and version-bound, stateless price confirmations."""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_GRAMS = 2**31 - 1  # Existing routing input representation, not a collection entitlement.
MAX_VOLUME_ML = 2**31 - 1  # Same existing routing input representation.
MAX_MONEY = 9007199254740991  # Existing customer money DTO's exact integer range.
WeightBand = Literal["BELOW_B", "B_TO_T", "ABOVE_T"]


def kilograms_to_grams(value: str) -> int:
    # No exponent, whitespace, sign, locale separator, binary float or silent rounding.
    if (
        not isinstance(value, str)
        or len(value) > 20
        or re.fullmatch(r"[0-9]+(?:\.[0-9]{1,3})?", value) is None
    ):
        raise ValueError("WEIGHT_KG_REQUIRES_AT_MOST_THREE_DECIMAL_PLACES")
    grams = int(Decimal(value) * 1000)
    if not 0 < grams <= MAX_GRAMS:
        raise ValueError("WEIGHT_OUT_OF_RANGE")
    return grams


class WeightPolicyFields(BaseModel):
    first_band_upper_grams: int = Field(strict=True, gt=0, le=MAX_GRAMS)
    numerical_threshold_grams: int = Field(strict=True, gt=0, le=MAX_GRAMS)
    first_band_label: str = Field(min_length=1, max_length=100)
    middle_band_label: str = Field(min_length=1, max_length=100)
    open_band_label: str = Field(min_length=1, max_length=100)
    display_unit: Literal["kg"]
    minimum_amount_minor: int = Field(strict=True, ge=100, le=MAX_MONEY)
    maximum_amount_minor: int = Field(strict=True, ge=100, le=MAX_MONEY)
    included_weight_grams: int = Field(strict=True, ge=0, le=MAX_GRAMS)
    saturation_weight_grams: int = Field(strict=True, gt=0, le=MAX_GRAMS)
    curvature: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    currency: Literal["INR"]
    active: bool = Field(strict=True)

    @field_validator("curvature", mode="before")
    @classmethod
    def exact_curvature(cls, value: object) -> Decimal:
        if (
            not isinstance(value, (str, Decimal))
            or re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", str(value)) is None
        ):
            raise ValueError("CURVATURE_REQUIRES_AN_EXACT_DECIMAL_STRING")
        return Decimal(value)

    @model_validator(mode="after")
    def ordered(self) -> "WeightPolicyFields":
        if self.numerical_threshold_grams <= self.first_band_upper_grams:
            raise ValueError("T_MUST_EXCEED_B")
        if self.maximum_amount_minor < self.minimum_amount_minor:
            raise ValueError("MAXIMUM_PRICE_BELOW_MINIMUM")
        if self.saturation_weight_grams <= self.included_weight_grams:
            raise ValueError("WMAX_MUST_EXCEED_W0")
        return self


class WeightQuoteConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_id: UUID
    policy_revision: int = Field(strict=True, ge=1)
    quote_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class WeightItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_category_code: str = Field(min_length=1, max_length=100)
    declared_quantity: int | None = Field(default=None, strict=True, ge=1, le=MAX_GRAMS)
    declared_weight_grams: int | None = Field(default=None, strict=True, ge=1, le=MAX_GRAMS)
    weight_band: WeightBand | None = None
    weight_kg: str | None = Field(default=None, max_length=20)
    # Occupied transport volume of the WHOLE line, including its selected quantity.
    declared_volume_ml: int | None = Field(default=None, strict=True, gt=0, le=MAX_VOLUME_ML)

    @field_validator("weight_kg")
    @classmethod
    def exact_weight(cls, value: str | None) -> str | None:
        if value is not None:
            kilograms_to_grams(value)
        return value


@dataclass(frozen=True, slots=True)
class WeightQuoteFacts:
    policy_id: UUID
    policy_revision: int
    total_weight_grams: int
    minimum_amount_minor: int
    maximum_amount_minor: int
    quote_fingerprint: str


class WeightPolicyResponse(WeightPolicyFields):
    policy_id: UUID
    policy_revision: int
    confirmation_freshness: Literal["CURRENT_POLICY_REVISION"] = "CURRENT_POLICY_REVISION"
    live_customer_holds_available: Literal[True] = True
    hold_blocker: None = None


class ValidatedWeightItem(WeightItemInput):
    declared_weight_grams: int = Field(strict=True, gt=0, le=MAX_GRAMS)
    weight_band: WeightBand


class WeightQuoteResponse(BaseModel):
    items: list[ValidatedWeightItem]
    total_weight_grams: int
    total_volume_ml: int | None = None
    minimum_amount_minor: int
    maximum_amount_minor: int
    total_amount_minor: int
    currency: Literal["INR"]
    confirmation: WeightQuoteConfirmation
    confirmation_freshness: Literal["CURRENT_POLICY_REVISION"] = "CURRENT_POLICY_REVISION"
    rounding: Literal["HALF_UP_MINOR_UNIT"] = "HALF_UP_MINOR_UNIT"
    live_customer_holds_available: bool
    hold_blocker: Literal["ITEM_VOLUME_DECLARATIONS_REQUIRED"] | None
