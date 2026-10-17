"""Relational regional ownership inputs and append-only planning outcomes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base


class ShiftConfirmation(Base):
    __tablename__ = "routing_shift_confirmation"
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"), primary_key=True)
    rider_id: Mapped[UUID] = mapped_column(ForeignKey("rider_profile.rider_id"))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlanningOutcome(Base):
    __tablename__ = "regional_planning_outcome"
    cycle_id: Mapped[UUID] = mapped_column(ForeignKey("fleet_cycle.cycle_id"), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_request.request_id"), primary_key=True
    )
    region_id: Mapped[UUID] = mapped_column(ForeignKey("operating_region.region_id"))
    generation: Mapped[int] = mapped_column(BigInteger)
    outcome: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(100))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_planning_generation"),
        CheckConstraint(
            "outcome IN ('ASSIGNED','EXISTING','DEFERRED','RECOVERY')", name="ck_planning_outcome"
        ),
        Index("ix_planning_outcome_request", "request_id", "evaluated_at"),
        Index("ix_planning_outcome_retry", "region_id", "retry_at"),
    )
