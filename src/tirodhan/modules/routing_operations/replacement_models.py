"""Immutable advisory evaluations; operational authority stays in existing route/custody tables."""

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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base


class ReplacementEvaluation(Base):
    __tablename__ = "replacement_evaluation"
    evaluation_id: Mapped[UUID] = mapped_column(primary_key=True)
    replacement_id: Mapped[UUID] = mapped_column(ForeignKey("rider_replacement.replacement_id"))
    replacement_version: Mapped[int] = mapped_column(BigInteger)
    region_version: Mapped[int] = mapped_column(BigInteger)
    source_version: Mapped[int] = mapped_column(BigInteger)
    source_fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(24))
    search: Mapped[dict[str, Any]] = mapped_column(JSONB)
    __table_args__ = (
        Index("ix_replacement_evaluation_history", "replacement_id", "created_at", "evaluation_id"),
    )


class ReplacementCandidate(Base):
    __tablename__ = "replacement_candidate"
    candidate_id: Mapped[UUID] = mapped_column(primary_key=True)
    evaluation_id: Mapped[UUID] = mapped_column(ForeignKey("replacement_evaluation.evaluation_id"))
    shift_id: Mapped[UUID] = mapped_column(ForeignKey("operating_shift.shift_id"))
    rank: Mapped[int] = mapped_column(BigInteger)
    problem: Mapped[dict[str, Any]] = mapped_column(JSONB)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("rank > 0", name="ck_replacement_candidate_rank"),
        UniqueConstraint("evaluation_id", "rank", name="uq_replacement_candidate_rank"),
        UniqueConstraint("evaluation_id", "shift_id", name="uq_replacement_candidate_shift"),
    )
