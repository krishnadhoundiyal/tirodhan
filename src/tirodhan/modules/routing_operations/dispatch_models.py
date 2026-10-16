"""Relational route authority; optimizer JSON documents are never execution state."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7


class RouteActivation(Base):
    __tablename__ = "route_activation"
    activation_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("routing_shadow_run.run_id"), unique=True)
    engine: Mapped[str] = mapped_column(String(16))
    activated_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "engine IN ('VROOM', 'OR_TOOLS', 'COMPOSITE')", name="ck_route_activation_engine"
        ),
    )


class VehicleRoute(Base):
    __tablename__ = "vehicle_route"
    route_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    vehicle_id: Mapped[UUID] = mapped_column(ForeignKey("operating_vehicle.vehicle_id"))
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"))
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("rider_assignment.assignment_id"), unique=True
    )
    version: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_vehicle_route_version"),
        CheckConstraint(
            "status IN ('ACTIVE', 'RECOVERY_REQUIRED', 'CLOSED')", name="ck_vehicle_route_status"
        ),
        CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL)", name="ck_vehicle_route_closed_at"
        ),
        Index(
            "uq_vehicle_route_open_vehicle",
            "vehicle_id",
            unique=True,
            postgresql_where=text("status <> 'CLOSED'"),
        ),
        Index(
            "uq_vehicle_route_open_shift",
            "shift_id",
            unique=True,
            postgresql_where=text("status <> 'CLOSED'"),
        ),
    )


class RouteRevision(Base):
    __tablename__ = "route_revision"
    revision_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    route_id: Mapped[UUID] = mapped_column(ForeignKey("vehicle_route.route_id"))
    activation_id: Mapped[UUID] = mapped_column(ForeignKey("route_activation.activation_id"))
    version: Mapped[int] = mapped_column(BigInteger)
    supersedes_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_revision.revision_id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("route_id", "version", name="uq_route_revision_version"),
        CheckConstraint("version > 0", name="ck_route_revision_version"),
    )


class RouteTask(Base):
    __tablename__ = "route_task"
    task_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    route_id: Mapped[UUID] = mapped_column(ForeignKey("vehicle_route.route_id"))
    stop_id: Mapped[UUID] = mapped_column()
    pickup_execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("pickup_execution.pickup_execution_id")
    )
    kind: Mapped[str] = mapped_column(String(16))
    receiving_point_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("receiving_point.receiving_point_id")
    )
    state: Mapped[str] = mapped_column(String(16))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("kind IN ('PICKUP', 'DISCHARGE')", name="ck_route_task_kind"),
        CheckConstraint(
            "committed_at IS NULL OR started_at IS NULL OR committed_at <= started_at",
            name="ck_task_commitment_time",
        ),
        CheckConstraint(
            "state IN ('PENDING', 'STARTED', 'COMPLETED', 'CANCELLED')", name="ck_route_task_state"
        ),
        CheckConstraint(
            "(state = 'PENDING' AND started_at IS NULL AND completed_at IS NULL) OR "
            "(state = 'STARTED' AND started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(state = 'COMPLETED' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND completed_at >= started_at) OR (state = 'CANCELLED' AND completed_at IS NULL)",
            name="ck_route_task_timestamps",
        ),
        CheckConstraint(
            "(kind = 'PICKUP' AND receiving_point_id IS NULL) OR "
            "(kind = 'DISCHARGE' AND receiving_point_id IS NOT NULL)",
            name="ck_route_task_destination",
        ),
        Index(
            "uq_route_task_live_stop",
            "route_id",
            "stop_id",
            unique=True,
            postgresql_where=text("state IN ('PENDING', 'STARTED')"),
        ),
    )


class RouteRevisionStop(Base):
    __tablename__ = "route_revision_stop"
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_revision.revision_id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("route_task.task_id"))
    planned_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    planned_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("revision_id", "task_id", name="uq_route_revision_task"),
        CheckConstraint(
            "ordinal >= 0 AND planned_end >= planned_start", name="ck_route_revision_stop_schedule"
        ),
    )


class RouteRecovery(Base):
    __tablename__ = "route_recovery"
    recovery_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    route_id: Mapped[UUID] = mapped_column(ForeignKey("vehicle_route.route_id"))
    manager_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    reason_code: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "reason_code IN ('VEHICLE_UNAVAILABLE', 'RIDER_UNAVAILABLE', 'EXECUTION_EXCEPTION')",
            name="ck_route_recovery_reason",
        ),
    )


class RouteDischargeProof(Base):
    __tablename__ = "route_discharge_proof"
    handover_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("handover_event.handover_event_id"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("route_task.task_id"))
    evidence_capture_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_capture.evidence_capture_id"), unique=True
    )
    receiving_identifier_snapshot: Mapped[str] = mapped_column(String(200))
    evidence_kind: Mapped[str] = mapped_column(String(16))
    __table_args__ = (CheckConstraint("evidence_kind = 'PHOTO'", name="ck_route_discharge_photo"),)
