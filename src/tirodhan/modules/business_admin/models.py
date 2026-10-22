"""Conventional current master data plus entity-specific revision relations.

Revision JSON contains historical row snapshots, never current operational authority.
Every history relation has a concrete FK to its own domain entity; there is no
polymorphic entity/type/value business store.
"""

from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Table,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base


class PickupPrice(Base):
    __tablename__ = "pickup_price"
    price_id: Mapped[UUID] = mapped_column(primary_key=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    active: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = (
        CheckConstraint("amount_minor >= 100 AND currency = 'INR'", name="ck_pickup_flat_price"),
        Index("uq_pickup_active_price", "active", unique=True, postgresql_where=text("active")),
    )


class WeightPricingPolicy(Base):
    __tablename__ = "weight_pricing_policy"
    policy_id: Mapped[UUID] = mapped_column(primary_key=True)
    first_band_upper_grams: Mapped[int] = mapped_column(BigInteger)
    numerical_threshold_grams: Mapped[int] = mapped_column(BigInteger)
    first_band_label: Mapped[str] = mapped_column(String(100))
    middle_band_label: Mapped[str] = mapped_column(String(100))
    open_band_label: Mapped[str] = mapped_column(String(100))
    display_unit: Mapped[str] = mapped_column(String(2))
    minimum_amount_minor: Mapped[int] = mapped_column(BigInteger)
    maximum_amount_minor: Mapped[int] = mapped_column(BigInteger)
    included_weight_grams: Mapped[int] = mapped_column(BigInteger)
    saturation_weight_grams: Mapped[int] = mapped_column(BigInteger)
    curvature: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    currency: Mapped[str] = mapped_column(String(3))
    active: Mapped[bool] = mapped_column(Boolean)
    adopted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    __table_args__ = (
        CheckConstraint(
            "first_band_upper_grams > 0 AND numerical_threshold_grams > first_band_upper_grams "
            "AND numerical_threshold_grams <= 2147483647",
            name="ck_weight_policy_bands",
        ),
        CheckConstraint(
            "minimum_amount_minor >= 100 AND maximum_amount_minor >= minimum_amount_minor "
            "AND maximum_amount_minor <= 9007199254740991 AND currency='INR' "
            "AND display_unit='kg' AND curvature > 0 AND included_weight_grams >= 0 "
            "AND saturation_weight_grams > included_weight_grams "
            "AND saturation_weight_grams <= 2147483647",
            name="ck_weight_policy_price",
        ),
        Index("uq_weight_policy_active", "active", unique=True, postgresql_where=text("active")),
    )


# Source PKs are explicit, not supplied by a caller. Shared capture mechanics do not
# replace relational domain identities. Child relations retain their own stable IDs.
REVISION_SOURCES = {
    "catalogue_group": "group_id",
    "catalogue_category": "category_id",
    "catalogue_media": "media_id",
    "catalogue_artwork": "artwork_id",
    "pickup_price": "price_id",
    "weight_pricing_policy": "policy_id",
    "rider_profile": "rider_id",
    "user_role": "user_role_id",
    "fleet": "fleet_id",
    "fleet_membership": "fleet_membership_id",
    "fleet_service_cell": "fleet_service_cell_id",
    "rider_service_cell": "rider_service_cell_id",
    "operating_vehicle": "vehicle_id",
    "operating_shift": "shift_id",
    "receiving_point": "receiving_point_id",
    "operating_region": "region_id",
    "operating_region_vehicle": "vehicle_id",
    "rider_assignment": "assignment_id",
    "fleet_control": "scope",
}

REVISIONS = {
    source: Table(
        f"{source}_revision",
        Base.metadata,
        Column(
            "entity_id",
            cast(Any, String(64) if source == "fleet_control" else PGUUID(as_uuid=True)),
            ForeignKey(f"{source}.{key}"),
            primary_key=True,
        ),
        Column("revision", BigInteger, primary_key=True),
        Column("previous_state", JSONB),
        Column("resulting_state", JSONB, nullable=False),
        Column("changed_fields", ARRAY(String), nullable=False),
        Column("actor_id", PGUUID(as_uuid=True), ForeignKey("app_user.user_id")),
        Column("command_id", PGUUID(as_uuid=True)),
        Column("reason", String(100)),
        Column("changed_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("revision > 0", name=f"ck_{source}_revision_positive"),
    )
    for source, key in REVISION_SOURCES.items()
}


class BookingWeightQuote(Base):
    """Immutable confirmed declared-weight/pricing provenance, independent of later observations."""

    __tablename__ = "booking_weight_quote"
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), primary_key=True
    )
    policy_id: Mapped[UUID] = mapped_column()
    policy_revision: Mapped[int] = mapped_column(BigInteger)
    total_weight_grams: Mapped[int] = mapped_column(BigInteger)
    minimum_amount_minor: Mapped[int] = mapped_column(BigInteger)
    maximum_amount_minor: Mapped[int] = mapped_column(BigInteger)
    quote_fingerprint: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(
            ["policy_id", "policy_revision"],
            ["weight_pricing_policy_revision.entity_id", "weight_pricing_policy_revision.revision"],
        ),
        CheckConstraint(
            "total_weight_grams > 0 AND total_weight_grams <= 2147483647 "
            "AND minimum_amount_minor >= 100 AND maximum_amount_minor >= minimum_amount_minor "
            "AND maximum_amount_minor <= 9007199254740991 AND policy_revision > 0",
            name="ck_booking_weight_quote",
        ),
    )
