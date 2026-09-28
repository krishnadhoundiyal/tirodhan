from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7


class PickupAttempt(Base):
    __tablename__ = "pickup_attempt"
    __table_args__ = (
        UniqueConstraint(
            "pickup_execution_id",
            "client_attempt_id",
            name="uq_pickup_attempt_client_attempt",
        ),
        UniqueConstraint(
            "pickup_execution_id",
            "attempt_number",
            name="uq_pickup_attempt_number",
        ),
        CheckConstraint("attempt_number > 0", name="ck_pickup_attempt_positive_number"),
        CheckConstraint(
            "outcome IN ('COLLECTED', 'NOT_COLLECTED')",
            name="ck_pickup_attempt_outcome",
        ),
    )

    pickup_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    pickup_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("pickup_execution.pickup_execution_id"),
        nullable=False,
    )
    rider_assignment_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("rider_assignment.assignment_id"),
        nullable=False,
    )
    client_attempt_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
