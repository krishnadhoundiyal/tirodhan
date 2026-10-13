from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7, utc_now


class OperatingVehicle(Base):
    __tablename__ = "operating_vehicle"
    __table_args__ = (
        CheckConstraint(
            "mass_capacity_g > 0 AND volume_capacity_ml > 0", name="ck_vehicle_capacity"
        ),
        CheckConstraint("version > 0", name="ck_vehicle_version"),
        CheckConstraint(
            "blocked_reason IS NULL OR blocked_reason IN "
            "('BREAKDOWN','LOAD_UNCERTAIN','DISCHARGE_DELAY','SAFETY')",
            name="ck_vehicle_block_reason",
        ),
    )
    vehicle_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    mass_capacity_g: Mapped[int] = mapped_column(BigInteger)
    volume_capacity_ml: Mapped[int] = mapped_column(BigInteger)
    profile_version: Mapped[str] = mapped_column(String(100))
    end_receiving_point_id: Mapped[UUID] = mapped_column(
        ForeignKey("receiving_point.receiving_point_id")
    )
    version: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    blocked_reason: Mapped[str | None] = mapped_column(String(24))
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class OperatingShift(Base):
    __tablename__ = "operating_shift"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_shift_window"),
        CheckConstraint("resource_kind IN ('COMMITTED', 'FLEXIBLE')", name="ck_shift_kind"),
        Index(
            "uq_open_vehicle_shift",
            "vehicle_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
        Index(
            "uq_open_rider_shift",
            "rider_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )
    shift_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    vehicle_id: Mapped[UUID] = mapped_column(ForeignKey("operating_vehicle.vehicle_id"))
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    resource_kind: Mapped[str] = mapped_column(String(16))
    commitment_id: Mapped[UUID] = mapped_column()
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class VehiclePosition(Base):
    __tablename__ = "vehicle_position"
    __table_args__ = (
        UniqueConstraint("vehicle_id", "revision", name="uq_vehicle_position_revision"),
        CheckConstraint("revision > 0", name="ck_position_revision"),
        CheckConstraint(
            "accuracy_m IS NULL OR accuracy_m BETWEEN 0 AND 1000", name="ck_position_accuracy"
        ),
        CheckConstraint(
            "session_id IS NULL OR accuracy_m IS NOT NULL", name="ck_position_provenance"
        ),
    )
    position_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    vehicle_id: Mapped[UUID] = mapped_column(ForeignKey("operating_vehicle.vehicle_id"))
    revision: Mapped[int] = mapped_column(BigInteger)
    location: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accuracy_m: Mapped[int | None] = mapped_column(Integer)
    session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("refresh_session.refresh_session_id")
    )
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RoutingManifest(Base):
    """Explicit physical reservation for one indivisible household lot; no inferred loads."""

    __tablename__ = "routing_manifest"
    __table_args__ = (
        CheckConstraint("mass_g > 0 AND volume_ml > 0", name="ck_manifest_load"),
        CheckConstraint(
            "pickup_seconds >= 0 AND discharge_seconds >= 0", name="ck_manifest_service"
        ),
    )
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), primary_key=True
    )
    mass_g: Mapped[int] = mapped_column(BigInteger)
    volume_ml: Mapped[int] = mapped_column(BigInteger)
    pickup_seconds: Mapped[int] = mapped_column(Integer)
    discharge_seconds: Mapped[int] = mapped_column(Integer)
    discharge_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    receiving_point_id: Mapped[UUID] = mapped_column(
        ForeignKey("receiving_point.receiving_point_id")
    )
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReceivingWindow(Base):
    __tablename__ = "routing_receiving_window"
    __table_args__ = (CheckConstraint("ends_at > starts_at", name="ck_routing_receiving_window"),)
    window_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    receiving_point_id: Mapped[UUID] = mapped_column(
        ForeignKey("receiving_point.receiving_point_id")
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CargoVehicleObservation(Base):
    """Attested legacy vehicle location of custody; does not transfer rider ownership."""

    __tablename__ = "cargo_vehicle_observation"
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), primary_key=True
    )
    vehicle_id: Mapped[UUID] = mapped_column(ForeignKey("operating_vehicle.vehicle_id"))
    pickup_attempt_id: Mapped[UUID] = mapped_column(ForeignKey("pickup_attempt.pickup_attempt_id"))
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RoutingShadowRun(Base):
    """Versioned immutable evaluation documents, never operational assignment truth."""

    __tablename__ = "routing_shadow_run"
    __table_args__ = (
        UniqueConstraint("requested_by", "client_command_id", name="uq_routing_shadow_command"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'STALE')",
            name="ck_shadow_status",
        ),
        CheckConstraint("attempt >= 0", name="ck_shadow_attempt"),
        CheckConstraint(
            "(status = 'RUNNING' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'RUNNING' AND lease_token IS NULL AND lease_until IS NULL)",
            name="ck_shadow_lease",
        ),
    )
    run_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    client_command_id: Mapped[UUID] = mapped_column()
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    problem: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary)
    business_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary)
    results: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
