"""Regional capacity ownership, replacement requests and advisory slot projections."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base


class RegionCapacityTransfer(Base):
    __tablename__ = "region_capacity_transfer"
    transfer_id: Mapped[UUID] = mapped_column(primary_key=True)
    vehicle_id: Mapped[UUID] = mapped_column(ForeignKey("operating_vehicle.vehicle_id"))
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    from_region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"))
    to_region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"))
    membership_version: Mapped[int] = mapped_column(BigInteger)
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    transferred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("from_region_id <> to_region_id", name="ck_capacity_transfer_regions"),
        Index("uq_capacity_transfer_version", "vehicle_id", "membership_version", unique=True),
    )


class RiderReplacement(Base):
    __tablename__ = "rider_replacement"
    replacement_id: Mapped[UUID] = mapped_column(primary_key=True)
    request_id: Mapped[UUID] = mapped_column(ForeignKey("collection_request.request_id"))
    original_rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"))
    status: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recommended_shift_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("operating_shift.shift_id")
    )
    source_version: Mapped[int | None] = mapped_column(BigInteger)
    recommended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.user_id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transfer_intent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cargo_transfer_intent.intent_id")
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    __table_args__ = (
        CheckConstraint(
            "status IN ('REQUESTED','RECOMMENDED','APPROVED','REJECTED','COMPLETED')",
            name="ck_replacement_status",
        ),
        Index(
            "uq_open_rider_replacement",
            "request_id",
            unique=True,
            postgresql_where=text("status NOT IN ('REJECTED','COMPLETED')"),
        ),
        Index("ix_replacement_region_due", "region_id", "next_attempt_at", "requested_at"),
    )


class ReplacementDecision(Base):
    __tablename__ = "rider_replacement_decision"
    decision_id: Mapped[UUID] = mapped_column(primary_key=True)
    replacement_id: Mapped[UUID] = mapped_column(ForeignKey("rider_replacement.replacement_id"))
    manager_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    decision: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(BigInteger)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RegionalSlotProjection(Base):
    __tablename__ = "regional_slot_projection"
    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_region.region_id"), primary_key=True
    )
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    slot_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_version: Mapped[int] = mapped_column(BigInteger)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    bookings: Mapped[int] = mapped_column(BigInteger)
    holds: Mapped[int] = mapped_column(BigInteger)
    vehicle_seconds: Mapped[int] = mapped_column(BigInteger)
    pickup_seconds: Mapped[int] = mapped_column(BigInteger)
    busy_seconds: Mapped[int] = mapped_column(BigInteger)
    generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    policy_version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    remaining_bookings: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    __table_args__ = (
        CheckConstraint(
            "status IN ('AVAILABLE','LIMITED','FULL','UNKNOWN')", name="ck_slot_status"
        ),
        CheckConstraint("slot_end > slot_start AND valid_until > computed_at", name="ck_slot_time"),
    )


class RegionalSlotRefresh(Base):
    __tablename__ = "regional_slot_refresh"
    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_region.region_id"), primary_key=True
    )
    source_version: Mapped[int] = mapped_column(BigInteger)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RegionalCapacityBudget(Base):
    __tablename__ = "regional_capacity_budget"
    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_region.region_id"), primary_key=True
    )

    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger)
    capacity_seconds: Mapped[int] = mapped_column(BigInteger)
    used_seconds: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (
        CheckConstraint("kind IN ('DUTY','PICKUP')", name="ck_budget_kind"),
        CheckConstraint(
            "generation > 0 AND capacity_seconds >= 0 AND used_seconds >= 0",
            name="ck_budget_values",
        ),
    )


class SlotAllowanceConfiguration(Base):
    __tablename__ = "regional_slot_policy"
    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_region.region_id"), primary_key=True
    )
    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB)
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    configured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("version > 0", name="ck_slot_policy_version"),)


class SlotReservationDebit(Base):
    __tablename__ = "slot_reservation_debit"
    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("regional_capacity_reservation.reservation_id"), primary_key=True
    )
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), primary_key=True)
    region_id: Mapped[UUID] = mapped_column()
    generation: Mapped[int] = mapped_column(BigInteger)
    seconds: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (
        ForeignKeyConstraint(
            ["region_id", "bucket_start", "kind"],
            [
                "regional_capacity_budget.region_id",
                "regional_capacity_budget.bucket_start",
                "regional_capacity_budget.kind",
            ],
        ),
        CheckConstraint("seconds > 0 AND generation > 0", name="ck_slot_debit_values"),
    )
