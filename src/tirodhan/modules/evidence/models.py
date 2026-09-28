from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
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
