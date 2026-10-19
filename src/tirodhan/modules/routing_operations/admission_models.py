"""Capacity proof history is advisory; existing operational/financial tables own facts."""

from __future__ import annotations

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
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7, utc_now


class AdmissionPlanRevision(Base):
    __tablename__ = "admission_plan_revision"
    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_region.region_id"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    format_version: Mapped[str] = mapped_column(String(64))
    source_stamp: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (CheckConstraint("revision > 0", name="ck_admission_plan_revision"),)


class AdmissionRouteCertificate(Base):
    __tablename__ = "admission_route_certificate"
    certificate_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"), index=True)
    vehicle_id: Mapped[UUID] = mapped_column(ForeignKey("operating_vehicle.vehicle_id"))
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"))
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    proof: Mapped[dict[str, Any]] = mapped_column(JSONB)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        UniqueConstraint(
            "region_id", "vehicle_id", "certificate_id", name="uq_admission_certificate_owner"
        ),
    )


class AdmissionRouteMembership(Base):
    __tablename__ = "admission_route_membership"
    region_id: Mapped[UUID] = mapped_column(primary_key=True)
    vehicle_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_vehicle.vehicle_id"), primary_key=True
    )
    from_revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    to_revision: Mapped[int | None] = mapped_column(BigInteger)
    certificate_id: Mapped[UUID] = mapped_column()
    __table_args__ = (
        ForeignKeyConstraint(
            ["region_id", "vehicle_id", "certificate_id"],
            [
                "admission_route_certificate.region_id",
                "admission_route_certificate.vehicle_id",
                "admission_route_certificate.certificate_id",
            ],
        ),
        ForeignKeyConstraint(
            ["region_id", "from_revision"],
            ["admission_plan_revision.region_id", "admission_plan_revision.revision"],
        ),
        ForeignKeyConstraint(
            ["region_id", "to_revision"],
            ["admission_plan_revision.region_id", "admission_plan_revision.revision"],
        ),
        CheckConstraint(
            "to_revision IS NULL OR to_revision > from_revision",
            name="ck_admission_membership_interval",
        ),
        Index(
            "uq_admission_current_route",
            "region_id",
            "vehicle_id",
            unique=True,
            postgresql_where=text("to_revision IS NULL"),
        ),
    )


class AdmissionObligation(Base):
    __tablename__ = "admission_obligation"
    obligation_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"), index=True)
    reservation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("regional_capacity_reservation.reservation_id"), unique=True
    )
    request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("collection_request.request_id"), unique=True
    )
    __table_args__ = (
        CheckConstraint(
            "reservation_id IS NOT NULL OR request_id IS NOT NULL",
            name="ck_admission_obligation_origin",
        ),
    )


class AdmissionAllocation(Base):
    __tablename__ = "admission_allocation"
    obligation_id: Mapped[UUID] = mapped_column(
        ForeignKey("admission_obligation.obligation_id"), primary_key=True
    )
    vehicle_id: Mapped[UUID] = mapped_column(ForeignKey("operating_vehicle.vehicle_id"), index=True)


class AdmissionProofObligation(Base):
    __tablename__ = "admission_proof_obligation"
    certificate_id: Mapped[UUID] = mapped_column(
        ForeignKey("admission_route_certificate.certificate_id"), primary_key=True
    )
    obligation_id: Mapped[UUID] = mapped_column(
        ForeignKey("admission_obligation.obligation_id"), primary_key=True
    )


class AdmissionPreparationLease(Base):
    __tablename__ = "admission_preparation_lease"
    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_region.region_id"), primary_key=True
    )
    token: Mapped[UUID] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AdmissionSourceHead(Base):
    __tablename__ = "admission_source_head"
    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_region.region_id"), primary_key=True
    )
    version: Mapped[int] = mapped_column(BigInteger, server_default="0")


class AdmissionSourceNotice(Base):
    __tablename__ = "admission_source_notice"
    notice_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"))
    source_table: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        Index("ix_admission_notice_region", "region_id", "notice_id"),
        UniqueConstraint(
            "region_id", "source_table", "source_key", name="uq_admission_source_invalidation"
        ),
    )
