"""Durable single-owner fleet control and typed operational exceptions."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7
from tirodhan.modules.business_admin.fact_metadata import BusinessFactMetadata


class FleetControl(Base):
    __tablename__ = "fleet_control"
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    region_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("operating_region.region_id"), unique=True
    )
    manager_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    version: Mapped[int] = mapped_column(BigInteger)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB)
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    business_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "(scope = 'COMMITTED_FLEET' AND region_id IS NULL) OR "
            "(region_id IS NOT NULL AND scope = 'REGION:' || region_id::text)",
            name="ck_fleet_scope",
        ),
        CheckConstraint("version > 0", name="ck_fleet_version"),
        CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)", name="ck_fleet_lease"),
    )


class FleetCycle(Base):
    __tablename__ = "fleet_cycle"
    cycle_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    scope: Mapped[str] = mapped_column(ForeignKey("fleet_control.scope"))
    control_version: Mapped[int] = mapped_column(BigInteger)
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    lease_token: Mapped[UUID] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(String(24))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("routing_shadow_run.run_id"))
    activation_id: Mapped[UUID | None] = mapped_column(ForeignKey("route_activation.activation_id"))
    selected_engine: Mapped[str | None] = mapped_column(String(16))
    measurements: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING','ACTIVATED','UNCHANGED','FAILED','STALE')",
            name="ck_fleet_cycle_status",
        ),
        Index("ix_fleet_cycle_created", "created_at"),
        CheckConstraint(
            "selected_engine IS NULL OR selected_engine IN ('VROOM','OR_TOOLS','COMPOSITE')",
            name="ck_fleet_selected_engine",
        ),
        CheckConstraint(
            "(status = 'RUNNING') = (completed_at IS NULL)", name="ck_fleet_cycle_completion"
        ),
        CheckConstraint(
            "status <> 'ACTIVATED' OR (activation_id IS NOT NULL AND selected_engine IS NOT NULL)",
            name="ck_fleet_cycle_authority",
        ),
    )


class FleetPositionAnchor(Base):
    __tablename__ = "fleet_position_anchor"
    vehicle_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_vehicle.vehicle_id"), primary_key=True
    )
    scope: Mapped[str] = mapped_column(ForeignKey("fleet_control.scope"))
    position_id: Mapped[UUID] = mapped_column(ForeignKey("vehicle_position.position_id"))


class FleetException(Base):
    __tablename__ = "fleet_exception"
    exception_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    cycle_id: Mapped[UUID | None] = mapped_column(ForeignKey("fleet_cycle.cycle_id"))
    request_id: Mapped[UUID | None] = mapped_column(ForeignKey("collection_request.request_id"))
    route_id: Mapped[UUID | None] = mapped_column(ForeignKey("vehicle_route.route_id"))
    reported_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    reason: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "cycle_id IS NOT NULL OR request_id IS NOT NULL OR route_id IS NOT NULL",
            name="ck_fleet_exception_target",
        ),
        Index("ix_fleet_exception_created", "created_at"),
    )


class FacilityRestriction(BusinessFactMetadata, Base):
    __tablename__ = "routing_facility_restriction"
    restriction_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    receiving_point_id: Mapped[UUID] = mapped_column(
        ForeignKey("receiving_point.receiving_point_id")
    )
    reason: Mapped[str] = mapped_column(String(24))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_facility_restriction_window"),
        CheckConstraint(
            "reason IN ('CLOSED','FULL','UNAVAILABLE')", name="ck_facility_restriction_reason"
        ),
        Index("ix_facility_restriction_point", "receiving_point_id", "starts_at", "ends_at"),
    )
