from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
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
from tirodhan.db.values import new_uuid7


class HandoverEvent(Base):
    __tablename__ = "handover_event"
    __table_args__ = (
        UniqueConstraint(
            "client_handover_id",
            name="uq_handover_event_client_handover",
        ),
        CheckConstraint(
            "status IN ('VALIDATED', 'REJECTED')",
            name="ck_handover_event_status",
        ),
        CheckConstraint(
            "validation_code IN ('WITHIN_ALLOWED_RADIUS', 'OUTSIDE_ALLOWED_RADIUS')",
            name="ck_handover_event_validation_code",
        ),
        CheckConstraint(
            "(status = 'VALIDATED' AND validation_code = 'WITHIN_ALLOWED_RADIUS') OR "
            "(status = 'REJECTED' AND validation_code = 'OUTSIDE_ALLOWED_RADIUS')",
            name="ck_handover_event_validation_consistency",
        ),
        CheckConstraint(
            "allowed_radius_m_snapshot > 0",
            name="ck_handover_event_positive_radius_snapshot",
        ),
        CheckConstraint("distance_m >= 0", name="ck_handover_event_nonnegative_distance"),
    )

    handover_event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    client_handover_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    rider_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("rider_profile.rider_id"), nullable=False
    )
    receiving_point_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("receiving_point.receiving_point_id"),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_location: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    receiving_point_location_snapshot: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    allowed_radius_m_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_m: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    validation_code: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HandoverEventItem(Base):
    __tablename__ = "handover_event_item"
    __table_args__ = (
        Index("ix_handover_item_pickup", "pickup_execution_id"),
        CheckConstraint(
            "status IN ('VALIDATED', 'REJECTED')",
            name="ck_handover_event_item_status",
        ),
        Index(
            "uq_handover_event_item_validated_pickup",
            "pickup_execution_id",
            unique=True,
            postgresql_where=text("status = 'VALIDATED'"),
        ),
    )

    handover_event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("handover_event.handover_event_id"),
        primary_key=True,
    )
    pickup_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("pickup_execution.pickup_execution_id"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
