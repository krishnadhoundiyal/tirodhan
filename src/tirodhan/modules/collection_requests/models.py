from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    CHAR,
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
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7, utc_now


class CollectionRequest(Base):
    __tablename__ = "collection_request"
    __table_args__ = (
        Index(
            "ix_customer_collection_page",
            "customer_id",
            text("created_at DESC"),
            text("request_id DESC"),
        ),
        UniqueConstraint(
            "customer_id", "client_request_id", name="uq_collection_request_customer_client"
        ),
        UniqueConstraint(
            "serviceability_context_id", name="uq_collection_request_serviceability_context"
        ),
        CheckConstraint("slot_end > slot_start", name="ck_collection_request_slot_order"),
        CheckConstraint("quoted_amount_minor >= 0", name="ck_collection_request_nonnegative_quote"),
        CheckConstraint(
            "payment_expires_at > created_at", name="ck_collection_request_payment_expiry"
        ),
    )

    request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    client_request_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("app_user.user_id"), nullable=False
    )
    serviceability_context_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("serviceability_context.serviceability_context_id"),
        nullable=False,
    )
    pickup_address_snapshot_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    pickup_location: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326), nullable=False
    )
    cell_id: Mapped[str] = mapped_column(String(200), nullable=False)
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    slot_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quoted_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payment_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planning_batch_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("planning_batch.planning_batch_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CollectionRequestItem(Base):
    __tablename__ = "collection_request_item"
    __table_args__ = (Index("ix_collection_item_request", "request_id"),)

    request_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("collection_request.request_id"), nullable=False
    )
    item_category_code: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    declared_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    declared_weight_grams: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quoted_line_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    pricing_rule_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
