from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7, utc_now


class ReceivingPoint(Base):
    __tablename__ = "receiving_point"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')",
            name="ck_receiving_point_status",
        ),
        CheckConstraint(
            "allowed_radius_m > 0",
            name="ck_receiving_point_positive_allowed_radius",
        ),
        CheckConstraint("version > 0", name="ck_receiving_point_positive_version"),
        Index(
            "ix_receiving_point_location_gist",
            "location",
            postgresql_using="gist",
        ),
    )

    receiving_point_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    official_name: Mapped[str] = mapped_column(String(200), nullable=False)
    official_identifier: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    allowed_radius_m: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
