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
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7, utc_now


class Fleet(Base):
    __tablename__ = "fleet"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="ck_fleet_status"),)

    fleet_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FleetMembership(Base):
    __tablename__ = "fleet_membership"
    __table_args__ = (
        Index(
            "uq_fleet_membership_current_rider",
            "rider_id",
            unique=True,
            postgresql_where=text("left_at IS NULL"),
        ),
    )

    fleet_membership_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    fleet_id: Mapped[UUID] = mapped_column(ForeignKey("fleet.fleet_id"), nullable=False)
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FleetServiceCell(Base):
    __tablename__ = "fleet_service_cell"
    __table_args__ = (
        Index(
            "uq_fleet_service_cell_active",
            "fleet_id",
            "cell_id",
            unique=True,
            postgresql_where=text("deactivated_at IS NULL"),
        ),
    )

    fleet_service_cell_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    fleet_id: Mapped[UUID] = mapped_column(ForeignKey("fleet.fleet_id"), nullable=False)
    cell_id: Mapped[str] = mapped_column(String(200), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RiderServiceCell(Base):
    __tablename__ = "rider_service_cell"
    __table_args__ = (
        Index(
            "uq_rider_service_cell_active",
            "rider_id",
            "cell_id",
            unique=True,
            postgresql_where=text("deactivated_at IS NULL"),
        ),
    )

    rider_service_cell_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"), nullable=False)
    cell_id: Mapped[str] = mapped_column(String(200), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PushRegistration(Base):
    __tablename__ = "push_registration"
    __table_args__ = (
        Index(
            "uq_push_registration_active_device",
            "rider_id",
            "client_device_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        CheckConstraint("provider = 'FCM'", name="ck_push_registration_provider"),
        CheckConstraint("platform IN ('ANDROID', 'IOS')", name="ck_push_registration_platform"),
    )

    push_registration_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"), nullable=False)
    client_device_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    registration_token: Mapped[str] = mapped_column(String(4096), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OfferNotificationDelivery(Base):
    __tablename__ = "offer_notification_delivery"
    __table_args__ = (
        UniqueConstraint(
            "offer_id", "push_registration_id", name="uq_offer_notification_delivery_device"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'PERMANENTLY_FAILED')",
            name="ck_offer_notification_delivery_status",
        ),
    )

    offer_notification_delivery_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    offer_id: Mapped[UUID] = mapped_column(ForeignKey("assignment_offer.offer_id"), nullable=False)
    push_registration_id: Mapped[UUID] = mapped_column(
        ForeignKey("push_registration.push_registration_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RiderProfile(Base):
    __tablename__ = "rider_profile"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED')", name="ck_rider_profile_status"),
    )

    rider_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("app_user.user_id"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    vehicle_type_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capacity_class_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class RiderAvailability(Base):
    __tablename__ = "rider_availability"
    __table_args__ = (
        CheckConstraint(
            "availability_intent IN ('OFFLINE', 'AVAILABLE')",
            name="ck_rider_availability_intent",
        ),
        CheckConstraint(
            "work_state IN ('IDLE', 'RESERVED', 'BUSY')",
            name="ck_rider_availability_work_state",
        ),
        CheckConstraint("version > 0", name="ck_rider_availability_positive_version"),
    )

    rider_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("rider_profile.rider_id"),
        primary_key=True,
    )
    availability_intent: Mapped[str] = mapped_column(String(16), nullable=False)
    work_state: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AssignmentOffer(Base):
    __tablename__ = "assignment_offer"
    __table_args__ = (
        UniqueConstraint(
            "collection_group_id",
            "rider_id",
            "offer_round",
            name="uq_assignment_offer_business_key",
        ),
        CheckConstraint("offer_round > 0", name="ck_assignment_offer_positive_round"),
        CheckConstraint("expires_at > offered_at", name="ck_assignment_offer_valid_expiry"),
        CheckConstraint(
            "status IN ('OPEN', 'ACCEPTED', 'CLOSED_LOST')",
            name="ck_assignment_offer_status",
        ),
        CheckConstraint(
            "(audience_kind = 'FLEET' AND fleet_id IS NOT NULL) OR "
            "(audience_kind = 'INDEPENDENT' AND fleet_id IS NULL)",
            name="ck_assignment_offer_audience",
        ),
    )

    offer_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    collection_group_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("collection_group.collection_group_id"),
        nullable=False,
    )
    rider_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("rider_profile.rider_id"), nullable=False
    )
    resolved_assignment_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("rider_assignment.assignment_id"), nullable=True
    )
    offer_round: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    offered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audience_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="INDEPENDENT", server_default="INDEPENDENT"
    )
    fleet_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("fleet.fleet_id")
    )


class RiderAssignment(Base):
    __tablename__ = "rider_assignment"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'COMPLETED', 'SUPERSEDED')",
            name="ck_rider_assignment_status",
        ),
        CheckConstraint(
            "source IN ('RIDER_OFFER_ACCEPTED', 'MANAGER_ASSIGNED')",
            name="ck_rider_assignment_source",
        ),
        CheckConstraint(
            "(source = 'MANAGER_ASSIGNED' AND assigned_by_user_id IS NOT NULL) OR "
            "(source = 'RIDER_OFFER_ACCEPTED' AND assigned_by_user_id IS NULL)",
            name="ck_rider_assignment_source_audit",
        ),
        Index(
            "uq_rider_assignment_active_group",
            "collection_group_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    assignment_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    collection_group_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("collection_group.collection_group_id"),
        nullable=False,
    )
    rider_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("rider_profile.rider_id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    assigned_by_user_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("app_user.user_id"), nullable=True
    )
    supersedes_assignment_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("rider_assignment.assignment_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RiderAssignmentItem(Base):
    __tablename__ = "rider_assignment_item"
    __table_args__ = (
        CheckConstraint(
            "release_reason_code IS NULL OR release_reason_code = 'REASSIGNED'",
            name="ck_rider_assignment_item_release_reason",
        ),
        CheckConstraint(
            "released_at IS NOT NULL OR release_reason_code IS NULL",
            name="ck_rider_assignment_item_release_consistency",
        ),
        Index(
            "uq_rider_assignment_item_active_pickup",
            "pickup_execution_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    assignment_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("rider_assignment.assignment_id"),
        primary_key=True,
    )
    pickup_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("pickup_execution.pickup_execution_id"),
        primary_key=True,
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
