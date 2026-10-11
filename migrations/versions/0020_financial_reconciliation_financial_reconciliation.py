"""financial_reconciliation

Revision ID: 0020_financial_reconciliation
Revises: 0019_customer_financial_events
Create Date: 2026-10-10 00:22:09.645997
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_financial_reconciliation"
down_revision: str | None = "0019_customer_financial_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payment_provider_event", sa.Column("observed_outcome", sa.String(24)))
    # Additive accounting only; preserve the three documented baseline objects.
    op.add_column(
        "payment",
        sa.Column(
            "cancellation_compensation_authorized",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.create_table(
        "captured_charge",
        sa.Column("captured_charge_id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("payment_attempt_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=200), nullable=False),
        sa.Column("provider_order_id", sa.String(length=200), nullable=True),
        sa.Column("provider_account_key", sa.String(length=64), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("evidence_id", sa.UUID(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_minor > 0", name="ck_captured_charge_amount"),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["payment_provider_event.payment_provider_event_id"],
        ),
        sa.ForeignKeyConstraint(
            ["payment_attempt_id"],
            ["payment_attempt.payment_attempt_id"],
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment.payment_id"],
        ),
        sa.PrimaryKeyConstraint("captured_charge_id"),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_captured_charge_identity"),
    )
    op.create_index("ix_captured_charge_payment", "captured_charge", ["payment_id"], unique=False)
    op.create_table(
        "refund_obligation",
        sa.Column("refund_obligation_id", sa.UUID(), nullable=False),
        sa.Column("captured_charge_id", sa.UUID(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("payout_blocked", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_minor > 0", name="ck_refund_obligation_amount"),
        sa.ForeignKeyConstraint(
            ["captured_charge_id"],
            ["captured_charge.captured_charge_id"],
        ),
        sa.PrimaryKeyConstraint("refund_obligation_id"),
        sa.UniqueConstraint("captured_charge_id", name="uq_refund_obligation_charge"),
    )
    op.create_table(
        "financial_audit",
        sa.Column("financial_audit_id", sa.UUID(), nullable=False),
        sa.Column("command_id", sa.UUID(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("refund_id", sa.UUID(), nullable=True),
        sa.Column("evidence_id", sa.UUID(), nullable=True),
        sa.Column("submitted_reference", sa.String(length=200), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("result_code", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_user.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["payment_provider_event.payment_provider_event_id"],
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment.payment_id"],
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"],
            ["refund.refund_id"],
        ),
        sa.PrimaryKeyConstraint("financial_audit_id"),
        sa.UniqueConstraint("command_id", name="uq_financial_audit_command"),
    )
    op.create_table(
        "financial_exception",
        sa.Column("financial_exception_id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("captured_charge_id", sa.UUID(), nullable=True),
        sa.Column("refund_id", sa.UUID(), nullable=True),
        sa.Column("evidence_id", sa.UUID(), nullable=True),
        sa.Column("case_key", sa.String(length=200), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["captured_charge_id"],
            ["captured_charge.captured_charge_id"],
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["payment_provider_event.payment_provider_event_id"],
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payment.payment_id"],
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"],
            ["refund.refund_id"],
        ),
        sa.PrimaryKeyConstraint("financial_exception_id"),
        sa.UniqueConstraint("case_key", name="uq_financial_exception_case"),
    )
    op.create_index(
        "ix_financial_exception_status",
        "financial_exception",
        ["status", "created_at"],
        unique=False,
    )
    op.add_column(
        "payment_attempt", sa.Column("provider_account_key", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "payment_attempt", sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "payment_attempt",
        sa.Column("check_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("payment_attempt", sa.Column("claim_token", sa.UUID(), nullable=True))
    op.add_column(
        "payment_attempt", sa.Column("claim_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_payment_attempt_reconciliation_due", "payment_attempt", ["next_check_at"], unique=False
    )
    op.add_column(
        "payment_provider_event",
        sa.Column(
            "evidence_source", sa.String(length=24), server_default="WEBHOOK", nullable=False
        ),
    )
    op.add_column(
        "payment_provider_event",
        sa.Column("provider_account_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "payment_provider_event",
        sa.Column("definitive_non_payable", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("refund", sa.Column("captured_charge_id", sa.UUID(), nullable=True))
    op.add_column("refund", sa.Column("refund_obligation_id", sa.UUID(), nullable=True))
    op.add_column("refund", sa.Column("failure_evidence_id", sa.UUID(), nullable=True))
    op.add_column(
        "refund", sa.Column("non_payable_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("refund", sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "refund", sa.Column("check_count", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("refund", sa.Column("claim_token", sa.UUID(), nullable=True))
    op.add_column("refund", sa.Column("claim_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_refund_reconciliation_due", "refund", ["next_check_at"], unique=False)
    op.create_foreign_key(
        "fk_refund_refund_obligation_id",
        "refund",
        "refund_obligation",
        ["refund_obligation_id"],
        ["refund_obligation_id"],
    )
    op.create_foreign_key(
        "fk_refund_captured_charge_id",
        "refund",
        "captured_charge",
        ["captured_charge_id"],
        ["captured_charge_id"],
    )
    op.create_foreign_key(
        "fk_refund_failure_evidence_id",
        "refund",
        "payment_provider_event",
        ["failure_evidence_id"],
        ["payment_provider_event_id"],
    )
    op.execute(
        "UPDATE payment_attempt SET next_check_at=created_at "
        "WHERE status IN ('CREATED','PENDING','INITIATION_UNCERTAIN')"
    )
    op.execute("UPDATE refund SET next_check_at=created_at WHERE status <> 'SUCCEEDED'")
    # Aggregate reservation bounds cannot be expressed as a row CHECK. This trigger
    # locks each charge and validates new intents. Provider outcome updates still
    # record external truth, including contradictions, rather than hiding payouts.
    op.execute("""
    CREATE FUNCTION enforce_charge_refund_reservation() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE c captured_charge%ROWTYPE; o refund_obligation%ROWTYPE; reserved bigint;
    BEGIN
      IF NEW.captured_charge_id IS NULL THEN RETURN NEW; END IF;
      SELECT * INTO c FROM captured_charge
        WHERE captured_charge_id=NEW.captured_charge_id FOR UPDATE;
      SELECT * INTO o FROM refund_obligation WHERE refund_obligation_id=NEW.refund_obligation_id;
      IF c.payment_id <> NEW.payment_id OR c.payment_attempt_id <> NEW.payment_attempt_id
         OR c.provider <> NEW.provider OR c.currency <> NEW.currency
         OR o.refund_obligation_id IS NULL OR o.captured_charge_id <> c.captured_charge_id
         OR o.payout_blocked OR o.amount_minor > c.amount_minor THEN
        RAISE EXCEPTION 'Invalid charge refund obligation' USING ERRCODE='23514';
      END IF;
      SELECT COALESCE(SUM(r.amount_minor),0) INTO reserved FROM refund r
      JOIN payment_attempt a ON a.payment_attempt_id=r.payment_attempt_id
      WHERE (r.captured_charge_id=c.captured_charge_id OR
        (r.captured_charge_id IS NULL AND a.payment_attempt_id=c.payment_attempt_id
          AND a.provider_payment_id=c.provider_payment_id))
      AND r.refund_id <> NEW.refund_id AND r.non_payable_verified_at IS NULL;
      IF reserved + NEW.amount_minor > c.amount_minor
         OR reserved + NEW.amount_minor > o.amount_minor THEN
        RAISE EXCEPTION 'Charge refund reservation exceeded' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER charge_refund_reservation BEFORE INSERT OR UPDATE OF amount_minor,
      captured_charge_id,refund_obligation_id,payment_id,payment_attempt_id,provider,currency
      ON refund FOR EACH ROW EXECUTE FUNCTION enforce_charge_refund_reservation();
    """)
    # ### end Alembic commands ###


def downgrade() -> None:
    op.drop_column("payment_provider_event", "observed_outcome")
    op.execute("DROP TRIGGER charge_refund_reservation ON refund")
    op.execute("DROP FUNCTION enforce_charge_refund_reservation()")
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint("fk_refund_failure_evidence_id", "refund", type_="foreignkey")
    op.drop_constraint("fk_refund_captured_charge_id", "refund", type_="foreignkey")
    op.drop_constraint("fk_refund_refund_obligation_id", "refund", type_="foreignkey")
    op.drop_index("ix_refund_reconciliation_due", table_name="refund")
    op.drop_column("refund", "claim_until")
    op.drop_column("refund", "claim_token")
    op.drop_column("refund", "check_count")
    op.drop_column("refund", "next_check_at")
    op.drop_column("refund", "non_payable_verified_at")
    op.drop_column("refund", "failure_evidence_id")
    op.drop_column("refund", "refund_obligation_id")
    op.drop_column("refund", "captured_charge_id")
    op.drop_column("payment_provider_event", "definitive_non_payable")
    op.drop_column("payment_provider_event", "provider_account_key")
    op.drop_column("payment_provider_event", "evidence_source")
    op.drop_index("ix_payment_attempt_reconciliation_due", table_name="payment_attempt")
    op.drop_column("payment_attempt", "claim_until")
    op.drop_column("payment_attempt", "claim_token")
    op.drop_column("payment_attempt", "check_count")
    op.drop_column("payment_attempt", "next_check_at")
    op.drop_column("payment_attempt", "provider_account_key")
    op.drop_index("ix_financial_exception_status", table_name="financial_exception")
    op.drop_table("financial_exception")
    op.drop_table("financial_audit")
    op.drop_table("refund_obligation")
    op.drop_index("ix_captured_charge_payment", table_name="captured_charge")
    op.drop_table("captured_charge")
    op.drop_column("payment", "cancellation_compensation_authorized")
    # ### end Alembic commands ###
