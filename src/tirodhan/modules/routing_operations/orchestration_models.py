"""Regional admission and operational history; no optimizer document grants authority."""

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


class OperatingRegion(Base):
    __tablename__ = "operating_region"
    region_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    default_warehouse_id: Mapped[UUID] = mapped_column(
        ForeignKey("receiving_point.receiving_point_id")
    )
    version: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (CheckConstraint("version > 0", name="ck_region_version"),)


class RegionCell(Base):
    __tablename__ = "operating_region_cell"
    cell_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"), index=True)


class RegionVehicle(Base):
    __tablename__ = "operating_region_vehicle"
    vehicle_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_vehicle.vehicle_id"), primary_key=True
    )
    region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"), index=True)
    version: Mapped[int] = mapped_column(BigInteger, server_default="1", default=1)


class RegionReceivingPoint(Base):
    __tablename__ = "operating_region_receiving_point"
    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("operating_region.region_id"), primary_key=True
    )
    receiving_point_id: Mapped[UUID] = mapped_column(
        ForeignKey("receiving_point.receiving_point_id"), primary_key=True
    )


class CapacityReservation(Base):
    __tablename__ = "regional_capacity_reservation"
    reservation_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"), index=True)
    region_version: Mapped[int] = mapped_column(BigInteger)
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    context_id: Mapped[UUID] = mapped_column(
        ForeignKey("serviceability_context.serviceability_context_id")
    )
    request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("collection_request.request_id"), index=True
    )
    pickup_location: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    slot_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mass_g: Mapped[int] = mapped_column(BigInteger)
    volume_ml: Mapped[int] = mapped_column(BigInteger)
    pickup_seconds: Mapped[int] = mapped_column(Integer)
    discharge_seconds: Mapped[int] = mapped_column(Integer)
    receiving_point_id: Mapped[UUID] = mapped_column(
        ForeignKey("receiving_point.receiving_point_id")
    )
    discharge_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    certificate: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        UniqueConstraint("region_id", "region_version", name="uq_region_capacity_version"),
        CheckConstraint("region_version > 0", name="ck_capacity_region_version"),
        CheckConstraint(
            "slot_end > slot_start AND expires_at > created_at", name="ck_reservation_times"
        ),
        CheckConstraint(
            "mass_g > 0 AND volume_ml > 0 AND pickup_seconds >= 0 AND discharge_seconds >= 0",
            name="ck_reservation_load",
        ),
    )


class FleetWakeup(Base):
    """One coalesced generation per scope; acknowledge only the generation actually read."""

    __tablename__ = "routing_fleet_wakeup"
    scope: Mapped[str] = mapped_column(String(100), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    acknowledged: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str] = mapped_column(String(50))
    __table_args__ = (
        CheckConstraint(
            "generation >= acknowledged AND acknowledged >= 0", name="ck_wakeup_generation"
        ),
    )


class RouteAcknowledgment(Base):
    __tablename__ = "route_acknowledgment"
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_revision.revision_id"), primary_key=True
    )
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    command_id: Mapped[UUID] = mapped_column(unique=True)
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RouteDelivery(Base):
    """Polling delivery has durable redelivery/ack deadlines; push is only an optional hint."""

    __tablename__ = "route_delivery"
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_revision.revision_id"), primary_key=True
    )
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    acknowledgment_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PickupReattempt(Base):
    __tablename__ = "pickup_reattempt"
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("pickup_attempt.pickup_attempt_id"), primary_key=True
    )
    reason: Mapped[str] = mapped_column(String(30))
    evidence_capture_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_capture.evidence_capture_id")
    )
    eligible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "reason IN ('CUSTOMER_UNAVAILABLE','UNCONTACTABLE','ACCESS_BLOCKED')",
            name="ck_reattempt_reason",
        ),
    )


class AppointmentConsent(Base):
    __tablename__ = "appointment_consent"
    consent_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), index=True
    )
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    slot_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("regional_capacity_reservation.reservation_id"), unique=True
    )
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("slot_end > slot_start", name="ck_consent_window"),)


class CargoTransfer(Base):
    __tablename__ = "cargo_transfer"
    transfer_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("cargo_transfer_intent.intent_id"), unique=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), index=True
    )
    from_vehicle_id: Mapped[UUID] = mapped_column(ForeignKey("operating_vehicle.vehicle_id"))
    to_vehicle_id: Mapped[UUID] = mapped_column(ForeignKey("operating_vehicle.vehicle_id"))
    from_rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    to_rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    donor_evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_capture.evidence_capture_id")
    )
    receiver_evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_capture.evidence_capture_id")
    )
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    confirmed_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    transferred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "from_vehicle_id <> to_vehicle_id AND from_rider_id <> to_rider_id AND sequence > 0",
            name="ck_cargo_transfer_resources",
        ),
        UniqueConstraint("request_id", "sequence", name="uq_cargo_transfer_sequence"),
    )


class DischargeAuthorization(Base):
    __tablename__ = "discharge_authorization"
    authorization_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), index=True
    )
    receiving_point_id: Mapped[UUID] = mapped_column(
        ForeignKey("receiving_point.receiving_point_id")
    )
    discharge_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OperatingBreak(Base):
    __tablename__ = "operating_break"
    break_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    __table_args__ = (CheckConstraint("ends_at > starts_at", name="ck_break_window"),)


class ShiftExtension(Base):
    __tablename__ = "operating_shift_extension"
    extension_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rider_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))

    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    safe_conditions_confirmed: Mapped[bool] = mapped_column(default=True, server_default="true")


class FlexibleRouteOffer(Base):
    __tablename__ = "flexible_route_offer"
    offer_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"))
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"))
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[UUID | None] = mapped_column(ForeignKey("rider_profile.rider_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CargoTransferIntent(Base):
    __tablename__ = "cargo_transfer_intent"
    intent_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    request_id: Mapped[UUID] = mapped_column(ForeignKey("collection_request.request_id"))
    from_vehicle_id: Mapped[UUID] = mapped_column(ForeignKey("operating_vehicle.vehicle_id"))
    to_shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"))
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(16))
    resolved_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.user_id"))
    resolution_evidence_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("evidence_capture.evidence_capture_id")
    )
    __table_args__ = (
        Index(
            "uq_open_cargo_transfer",
            "request_id",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
        CheckConstraint(
            "(resolved_at IS NULL AND resolution IS NULL AND resolved_by IS NULL "
            "AND resolution_evidence_id IS NULL) OR (resolved_at IS NOT NULL "
            "AND resolved_by IS NOT NULL AND resolution IS NOT NULL "
            "AND (resolution='TRANSFERRED' OR "
            "(resolution='ABORTED' AND resolution_evidence_id IS NOT NULL)))",
            name="ck_transfer_resolution",
        ),
    )


class CargoTransferReceipt(Base):
    __tablename__ = "cargo_transfer_receipt"
    intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("cargo_transfer_intent.intent_id"), primary_key=True
    )
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"), primary_key=True)
    capture_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_capture.evidence_capture_id"), unique=True
    )
    witnessed_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.user_id"))
    evidence_media_type: Mapped[str] = mapped_column(
        String(16), default="PHOTO", server_default="PHOTO"
    )
    __table_args__ = (
        CheckConstraint("evidence_media_type = 'PHOTO'", name="ck_transfer_receipt_photo"),
    )


class FlexibleCommitment(Base):
    __tablename__ = "flexible_commitment"
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), primary_key=True
    )
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"), index=True)
    offer_id: Mapped[UUID] = mapped_column(ForeignKey("flexible_route_offer.offer_id"), unique=True)
    accepted_by: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShiftExtensionConsent(Base):
    __tablename__ = "shift_extension_consent"
    consent_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"))
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CapacityAdmissionRequest(Base):
    __tablename__ = "capacity_admission_request"
    admission_id: Mapped[UUID] = mapped_column(primary_key=True)
    region_id: Mapped[UUID | None] = mapped_column(ForeignKey("operating_region.region_id"))
    reattempt_request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("collection_request.request_id")
    )
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    context_id: Mapped[UUID] = mapped_column(
        ForeignKey("serviceability_context.serviceability_context_id")
    )
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    slot_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mass_g: Mapped[int] = mapped_column(BigInteger)
    volume_ml: Mapped[int] = mapped_column(BigInteger)
    pickup_seconds: Mapped[int] = mapped_column(Integer)
    discharge_seconds: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preparation_cursor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        Index("ix_capacity_admission_due", "status", "created_at", "admission_id"),
        Index("ix_capacity_admission_lease", "status", "lease_until"),
        Index("ix_capacity_admission_retry", "status", "next_attempt_at"),
        Index("ix_admission_regional_due", "region_id", "status", "created_at", "admission_id"),
        Index(
            "uq_capacity_admission_region_running",
            "region_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','NEEDS_ACCEPTANCE')",
            name="ck_admission_status",
        ),
        CheckConstraint(
            "slot_end > slot_start AND expires_at > created_at AND mass_g > 0 "
            "AND volume_ml > 0 AND pickup_seconds >= 0 AND discharge_seconds >= 0",
            name="ck_admission_inputs",
        ),
    )


class CustomerRouteETA(Base):
    __tablename__ = "customer_route_eta"
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), primary_key=True
    )
    revision_id: Mapped[UUID] = mapped_column(ForeignKey("route_revision.revision_id"))
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    service_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fresh_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdmissionFlexibleOffer(Base):
    __tablename__ = "admission_flexible_offer"
    offer_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    admission_id: Mapped[UUID] = mapped_column(
        ForeignKey("capacity_admission_request.admission_id")
    )
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("admission_id", "shift_id", name="uq_admission_flexible_offer"),
    )


class AdmissionFlexibleCommitment(Base):
    __tablename__ = "admission_flexible_commitment"
    admission_id: Mapped[UUID] = mapped_column(
        ForeignKey("capacity_admission_request.admission_id"), primary_key=True
    )
    offer_id: Mapped[UUID] = mapped_column(
        ForeignKey("admission_flexible_offer.offer_id"), unique=True
    )
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"), index=True)
    accepted_by: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReattemptAuthorization(Base):
    __tablename__ = "reattempt_authorization"
    authorization_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), index=True
    )
    after_attempt_id: Mapped[UUID] = mapped_column(ForeignKey("pickup_attempt.pickup_attempt_id"))
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PhysicalLoadObservation(Base):
    __tablename__ = "physical_load_observation"
    observation_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), index=True
    )
    mass_g: Mapped[int] = mapped_column(BigInteger)
    volume_ml: Mapped[int] = mapped_column(BigInteger)
    capture_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence_capture.evidence_capture_id"), unique=True
    )
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence_media_type: Mapped[str] = mapped_column(
        String(16), default="PHOTO", server_default="PHOTO"
    )
    __table_args__ = (
        CheckConstraint(
            "mass_g > 0 AND volume_ml > 0 AND evidence_media_type = 'PHOTO'",
            name="ck_observed_load",
        ),
    )


class RouteResumeAuthorization(Base):
    __tablename__ = "route_resume_authorization"
    authorization_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    revision_id: Mapped[UUID] = mapped_column(ForeignKey("route_revision.revision_id"), index=True)
    vehicle_safety_version: Mapped[int] = mapped_column(BigInteger)
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
