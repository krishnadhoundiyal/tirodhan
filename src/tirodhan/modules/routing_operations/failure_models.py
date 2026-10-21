"""Concrete operational incident, allocation and immutable recovery proof relations."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base


class RecoveryWorkRequest(Base):
    """One durable notification generation, separate from operational attempt state."""

    __tablename__ = "recovery_work_request"
    outbox_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("outbox_event.outbox_event_id"), primary_key=True
    )
    assignment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("recovery_assignment.assignment_id")
    )
    replacement_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rider_replacement.replacement_id")
    )
    generation: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(24))
    publication_token: Mapped[UUID | None] = mapped_column()
    publication_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (
        CheckConstraint(
            "(assignment_id IS NOT NULL) <> (replacement_id IS NOT NULL)",
            name="ck_recovery_work_owner",
        ),
        CheckConstraint("generation > 0", name="ck_recovery_work_generation"),
        CheckConstraint(
            "state IN ('READY','PUBLISHING','PUBLISHED','RETRY_DUE','CLOSED')",
            name="ck_recovery_work_state",
        ),
        CheckConstraint(
            "(state = 'PUBLISHING') = (publication_token IS NOT NULL) AND "
            "(publication_token IS NULL) = (publication_until IS NULL)",
            name="ck_recovery_work_lease",
        ),
        CheckConstraint(
            "(state = 'CLOSED') = (closed_at IS NOT NULL)", name="ck_recovery_work_closed"
        ),
        UniqueConstraint(
            "assignment_id", "generation", name="uq_recovery_work_assignment_generation"
        ),
        UniqueConstraint(
            "replacement_id", "generation", name="uq_recovery_work_replacement_generation"
        ),
        Index(
            "uq_recovery_work_assignment_open",
            "assignment_id",
            unique=True,
            postgresql_where=text("state <> 'CLOSED'"),
        ),
        Index(
            "uq_recovery_work_replacement_open",
            "replacement_id",
            unique=True,
            postgresql_where=text("state <> 'CLOSED'"),
        ),
    )


class RiderFailureIncident(Base):
    __tablename__ = "rider_failure_incident"
    incident_id: Mapped[UUID] = mapped_column(primary_key=True)
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"))
    reported_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    reason: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    version: Mapped[int] = mapped_column(BigInteger, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN','RESOLVED') AND version > 0", name="ck_failure_incident_state"
        ),
        Index(
            "uq_failure_open_rider",
            "rider_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )


class FailureRoute(Base):
    __tablename__ = "rider_failure_route"
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("rider_failure_incident.incident_id"), primary_key=True
    )
    route_id: Mapped[UUID] = mapped_column(ForeignKey("vehicle_route.route_id"), primary_key=True)
    revision_id: Mapped[UUID] = mapped_column(ForeignKey("route_revision.revision_id"))


class RecoveryAssignment(Base):
    __tablename__ = "recovery_assignment"
    assignment_id: Mapped[UUID] = mapped_column(primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("rider_failure_incident.incident_id"))
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("replacement_candidate.candidate_id"))
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"))
    approved_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    version: Mapped[int] = mapped_column(BigInteger, default=1)
    attempt: Mapped[int] = mapped_column(BigInteger, default=0)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    activation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_activation.activation_id", use_alter=True)
    )
    __table_args__ = (
        Index("ix_recovery_incident", "incident_id", "assignment_id"),
        Index(
            "uq_recovery_pending_shift",
            "shift_id",
            unique=True,
            postgresql_where=text(
                "status IN ('PENDING','RUNNING','AWAITING_TRANSFER','ATTENTION')"
            ),
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','AWAITING_TRANSFER','ACTIVATED',"
            "'ATTENTION','SUPERSEDED')",
            name="ck_recovery_assignment_state",
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_until IS NULL)", name="ck_recovery_assignment_lease"
        ),
    )


class RecoveryAssignmentItem(Base):
    __tablename__ = "recovery_assignment_item"
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("recovery_assignment.assignment_id"), primary_key=True
    )
    replacement_id: Mapped[UUID] = mapped_column(
        ForeignKey("rider_replacement.replacement_id"), primary_key=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index(
            "uq_recovery_item_active",
            "replacement_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )


class RecoveryWitness(Base):
    __tablename__ = "recovery_witness"
    witness_id: Mapped[UUID] = mapped_column(primary_key=True)
    assignment_id: Mapped[UUID] = mapped_column(ForeignKey("recovery_assignment.assignment_id"))
    source_fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB)
    problem: Mapped[dict[str, Any]] = mapped_column(JSONB)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RoutePushDelivery(Base):
    __tablename__ = "route_push_delivery"
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_revision.revision_id"), primary_key=True
    )
    registration_id: Mapped[UUID] = mapped_column(
        ForeignKey("push_registration.push_registration_id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(24))
    attempts: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','SENT','PERMANENTLY_FAILED') AND attempts >= 0",
            name="ck_route_push_status",
        ),
    )
