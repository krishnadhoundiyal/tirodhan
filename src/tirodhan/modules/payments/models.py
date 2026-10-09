from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
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


class Payment(Base):
    __tablename__ = "payment"
    __table_args__ = (UniqueConstraint("request_id", name="uq_payment_request"),)

    payment_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    request_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("collection_request.request_id"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    successful_attempt_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_compensation_authorized: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )


class PaymentAttempt(Base):
    __tablename__ = "payment_attempt"
    __table_args__ = (
        Index("ix_payment_attempt_payment_created", "payment_id", "created_at"),
        Index("ix_payment_attempt_reconciliation_due", "next_check_at"),
        UniqueConstraint(
            "provider", "provider_idempotency_key", name="uq_payment_attempt_provider_key"
        ),
        Index(
            "uq_payment_attempt_provider_order",
            "provider",
            "provider_order_id",
            unique=True,
            postgresql_where=text("provider_order_id IS NOT NULL"),
        ),
        Index(
            "uq_payment_attempt_provider_payment",
            "provider",
            "provider_payment_id",
            unique=True,
            postgresql_where=text("provider_payment_id IS NOT NULL"),
        ),
    )

    payment_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    payment_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("payment.payment_id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_account_key: Mapped[str | None] = mapped_column(String(64))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utc_now)
    check_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claim_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    claim_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentProviderEvent(Base):
    __tablename__ = "payment_provider_event"
    __table_args__ = (
        UniqueConstraint(
            "provider", "external_event_id", name="uq_payment_provider_event_identity"
        ),
        CheckConstraint(
            "amount_minor IS NULL OR amount_minor > 0", name="ck_provider_event_amount"
        ),
    )

    payment_provider_event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payment_attempt_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("payment_attempt.payment_attempt_id"),
        nullable=True,
    )
    refund_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "refund.refund_id", use_alter=True, name="payment_provider_event_refund_id_fkey"
        ),
        nullable=True,
    )
    payload_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    processing_status: Mapped[str] = mapped_column(String(24), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Authenticated capture facts survive even when a second charge cannot be
    # assigned as canonical. Never store a raw provider payload here.
    provider_payment_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    observed_outcome: Mapped[str | None] = mapped_column(String(24))
    evidence_source: Mapped[str] = mapped_column(
        String(24), default="WEBHOOK", server_default="WEBHOOK"
    )
    provider_account_key: Mapped[str | None] = mapped_column(String(64))
    definitive_non_payable: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )


class Refund(Base):
    __tablename__ = "refund"
    __table_args__ = (
        Index("ix_refund_payment_created", "payment_id", "created_at"),
        Index("ix_refund_reconciliation_due", "next_check_at"),
        UniqueConstraint("provider", "provider_idempotency_key", name="uq_refund_provider_key"),
        Index(
            "uq_refund_provider_refund",
            "provider",
            "provider_refund_id",
            unique=True,
            postgresql_where=text("provider_refund_id IS NOT NULL"),
        ),
        CheckConstraint("amount_minor > 0", name="ck_refund_amount_positive"),
    )

    refund_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    payment_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("payment.payment_id"), nullable=False
    )
    payment_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("payment_attempt.payment_attempt_id"),
        nullable=False,
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_refund_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    requested_by_user_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("app_user.user_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_charge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("captured_charge.captured_charge_id")
    )
    refund_obligation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("refund_obligation.refund_obligation_id")
    )
    failure_evidence_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "payment_provider_event.payment_provider_event_id",
            name="fk_refund_failure_evidence_id",
            use_alter=True,
        )
    )
    non_payable_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utc_now)
    check_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claim_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    claim_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CapturedCharge(Base):
    __tablename__ = "captured_charge"
    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_captured_charge_identity"),
        CheckConstraint("amount_minor > 0", name="ck_captured_charge_amount"),
        Index("ix_captured_charge_payment", "payment_id"),
    )
    captured_charge_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payment.payment_id"))
    payment_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("payment_attempt.payment_attempt_id")
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_payment_id: Mapped[str] = mapped_column(String(200))
    provider_order_id: Mapped[str | None] = mapped_column(String(200))
    provider_account_key: Mapped[str | None] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(CHAR(3))
    evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("payment_provider_event.payment_provider_event_id")
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RefundObligation(Base):
    __tablename__ = "refund_obligation"
    __table_args__ = (
        UniqueConstraint("captured_charge_id", name="uq_refund_obligation_charge"),
        CheckConstraint("amount_minor > 0", name="ck_refund_obligation_amount"),
    )
    refund_obligation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    captured_charge_id: Mapped[UUID] = mapped_column(
        ForeignKey("captured_charge.captured_charge_id")
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    reason_code: Mapped[str] = mapped_column(String(64))
    payout_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FinancialException(Base):
    __tablename__ = "financial_exception"
    __table_args__ = (
        UniqueConstraint("case_key", name="uq_financial_exception_case"),
        Index("ix_financial_exception_status", "status", "created_at"),
    )
    financial_exception_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payment.payment_id"))
    captured_charge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("captured_charge.captured_charge_id")
    )
    refund_id: Mapped[UUID | None] = mapped_column(ForeignKey("refund.refund_id"))
    evidence_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("payment_provider_event.payment_provider_event_id")
    )
    case_key: Mapped[str] = mapped_column(String(200))
    reason_code: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FinancialAudit(Base):
    __tablename__ = "financial_audit"
    __table_args__ = (UniqueConstraint("command_id", name="uq_financial_audit_command"),)
    financial_audit_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    command_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payment.payment_id"))
    refund_id: Mapped[UUID | None] = mapped_column(ForeignKey("refund.refund_id"))
    evidence_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("payment_provider_event.payment_provider_event_id")
    )
    submitted_reference: Mapped[str | None] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(64))
    result_code: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
