from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7


class EvidenceCapture(Base):
    __tablename__ = "evidence_capture"
    __table_args__ = (
        UniqueConstraint(
            "client_capture_id",
            name="uq_evidence_capture_client_capture",
        ),
    )

    evidence_capture_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    client_capture_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    captured_by_user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("app_user.user_id"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PickupEvidenceLink(Base):
    __tablename__ = "pickup_evidence_link"
    __table_args__ = (
        UniqueConstraint(
            "evidence_capture_id",
            name="uq_pickup_evidence_link_capture",
        ),
    )

    pickup_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("pickup_execution.pickup_execution_id"),
        primary_key=True,
    )
    evidence_capture_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("evidence_capture.evidence_capture_id"),
        primary_key=True,
    )


class HandoverEvidenceLink(Base):
    __tablename__ = "handover_evidence_link"
    __table_args__ = (
        UniqueConstraint(
            "evidence_capture_id",
            name="uq_handover_evidence_link_capture",
        ),
    )

    handover_event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("handover_event.handover_event_id"),
        primary_key=True,
    )
    evidence_capture_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("evidence_capture.evidence_capture_id"),
        primary_key=True,
    )


class MediaAsset(Base):
    __tablename__ = "media_asset"
    __table_args__ = (
        UniqueConstraint("client_media_id", name="uq_media_asset_client_media"),
        UniqueConstraint("evidence_capture_id", name="uq_media_asset_evidence_capture"),
        UniqueConstraint("object_key", name="uq_media_asset_object_key"),
        CheckConstraint(
            "media_type IN ('PHOTO', 'VIDEO')",
            name="ck_media_asset_media_type",
        ),
        CheckConstraint(
            "upload_status IN ('PENDING_UPLOAD', 'FINALIZED')",
            name="ck_media_asset_upload_status",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_media_asset_nonnegative_size",
        ),
        CheckConstraint(
            "(upload_status = 'PENDING_UPLOAD' "
            "AND stored_content_type IS NULL "
            "AND size_bytes IS NULL "
            "AND finalized_at IS NULL) "
            "OR (upload_status = 'FINALIZED' "
            "AND stored_content_type IS NOT NULL "
            "AND size_bytes IS NOT NULL "
            "AND finalized_at IS NOT NULL)",
            name="ck_media_asset_lifecycle_consistency",
        ),
    )

    media_asset_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    client_media_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    evidence_capture_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("evidence_capture.evidence_capture_id"),
        nullable=False,
    )
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    object_key: Mapped[str] = mapped_column(String(80), nullable=False)
    expected_content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_content_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    upload_status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
