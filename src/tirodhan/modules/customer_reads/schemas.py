from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class MediaDto(BaseModel):
    url: str
    thumbnail_url: str | None
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    alt_text: str = Field(min_length=1)
    blurhash: str | None
    expires_at: datetime | None

    @field_validator("url", "thumbnail_url")
    @classmethod
    def safe_url(cls, value: str | None) -> str | None:
        from urllib.parse import parse_qs, urlsplit

        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("media requires safe HTTPS")
        # Product reads must never expose write-capable Azure SAS credentials.
        permissions = parse_qs(parsed.query).get("sp", [])
        if permissions and any(permission != "r" for permission in permissions):
            raise ValueError("media authorization must be read-only")
        return value


class CatalogueGroupDto(BaseModel):
    group_code: str
    display_name: str
    display_order: int
    active: bool


class CategoryInputDto(BaseModel):
    quantity: Literal["NONE", "OPTIONAL"]
    weight_grams: Literal["NONE", "OPTIONAL"]


class CatalogueCategoryDto(BaseModel):
    category_code: str
    group_code: str
    display_name: str
    description: str
    display_order: int
    active: bool
    image: MediaDto
    thumbnail: MediaDto
    handling_hints: str | None
    input: CategoryInputDto


class QuickCategoryDto(BaseModel):
    category_code: str
    label: str
    display_order: int


class ArtworkDto(BaseModel):
    hero: MediaDto | None = None
    home: MediaDto | None = None
    rickshaw: MediaDto | None = None
    receiving_point: MediaDto | None = None


class CatalogueDto(BaseModel):
    version: str
    groups: list[CatalogueGroupDto]
    categories: list[CatalogueCategoryDto]
    quick_categories: list[QuickCategoryDto]
    artwork: ArtworkDto


class SlotDto(BaseModel):
    start: datetime
    end: datetime
    label: str


class OfferedSlotDto(SlotDto):
    slot_id: str
    availability: Literal["AVAILABLE", "FULL"]


class SlotsDto(BaseModel):
    serviceability_context_id: UUID
    expires_at: datetime
    slots: list[OfferedSlotDto]


class MoneyDto(BaseModel):
    amount_minor: int = Field(ge=0, le=9007199254740991)
    currency: str


class PaymentAttemptDto(BaseModel):
    payment_attempt_id: UUID
    status: Literal["PENDING", "PROCESSING", "SUCCEEDED", "FAILED", "CONFIRMING"]


class PaymentDto(MoneyDto):
    payment_id: UUID
    request_id: UUID
    status: Literal[
        "PENDING", "PROCESSING", "SUCCEEDED", "FAILED", "CONFIRMING", "CANCELLED", "EXPIRED"
    ]
    retry_allowed: bool
    expires_at: datetime | None
    succeeded_at: datetime | None
    current_attempt: PaymentAttemptDto | None


RefundStatus = Literal["INITIATED", "PROCESSING", "COMPLETED", "CONFIRMING", "FAILED"]


class RefundDto(MoneyDto):
    refund_id: UUID
    status: RefundStatus
    initiated_at: datetime
    completed_at: datetime | None
    reason: Literal["CUSTOMER_CANCELLATION", "SERVICE_UNAVAILABLE", "PAYMENT_CORRECTION"]


class CancellationDto(BaseModel):
    allowed: bool
    cutoff_at: datetime | None
    reason: (
        Literal[
            "PLANNING_CUTOFF_REACHED",
            "PLANNING_STARTED",
            "NOT_ACCEPTED",
            "ALREADY_CANCELLED",
            "NOT_ELIGIBLE",
        ]
        | None
    )
    refund_expectation: Literal["NONE", "FULL_PAYMENT", "REVIEW_REQUIRED"]


class MilestoneDto(BaseModel):
    code: Literal["BOOKED", "COLLECTED", "RECEIVED", "HANDOVER_VALIDATED"]
    state: Literal["COMPLETE", "CURRENT", "UPCOMING"]
    occurred_at: datetime | None
    label: str
    detail: str | None = None
    image: MediaDto | None = None


class ReceivingPointDto(BaseModel):
    name: str
    address_summary: str
    authority_label: str
    image: MediaDto | None = None


class HandoverDto(BaseModel):
    state: Literal["NOT_RECORDED", "RECORDED", "VALIDATED"]
    recorded_at: datetime | None
    validated_at: datetime | None


class JourneyDto(BaseModel):
    request_id: UUID
    milestones: list[MilestoneDto]
    receiving_point: ReceivingPointDto | None
    handover: HandoverDto


class CollectionSummaryDto(BaseModel):
    request_id: UUID
    status: Literal[
        "PENDING_PAYMENT",
        "ACCEPTED",
        "PRE_PLANNING",
        "PLANNED",
        "CANCELLED",
        "COMPLETED",
        "EXPIRED",
    ]
    title: str
    slot: SlotDto
    address_summary: str
    category_codes: list[str]
    image: MediaDto | None
    journey_status: Literal["BOOKED", "COLLECTED", "RECEIVED", "HANDOVER_VALIDATED", "NOT_STARTED"]
    refund_status: RefundStatus | None
    created_at: datetime
    updated_at: datetime


class AddressDto(BaseModel):
    label: str | None
    text: str


class CollectionItemDto(BaseModel):
    category_code: str
    display_name: str
    declared_quantity: int | None
    declared_weight_grams: int | None
    quoted_line_amount_minor: int
    image: MediaDto | None


class CollectionDetailDto(CollectionSummaryDto):
    address: AddressDto
    items: list[CollectionItemDto]
    quote: MoneyDto
    payment: PaymentDto
    cancellation: CancellationDto
    journey: JourneyDto
    refunds: list[RefundDto]
    cancelled_at: datetime | None
    completed_at: datetime | None


class CollectionPageDto(BaseModel):
    items: list[CollectionSummaryDto]
    next_cursor: str | None


class RecommendationItemDto(BaseModel):
    category_code: str
    rank: int
    reason: Literal["PREVIOUS_COLLECTION"] = "PREVIOUS_COLLECTION"


class RecommendationsDto(BaseModel):
    recommendations: list[RecommendationItemDto]
