"""Recurring attention and typed audit for evidence awaiting Payment matching."""

import sqlalchemy as sa
from alembic import op

revision = "0022_financial_review_recovery"
down_revision = "0021_financial_correctness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "financial_exception",
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "financial_exception", sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE financial_exception SET detected_at=created_at")
    op.alter_column("financial_exception", "detected_at", nullable=False)
    op.add_column("financial_exception", sa.Column("last_escalated_at", sa.DateTime(timezone=True)))
    op.alter_column("financial_audit", "payment_id", existing_type=sa.UUID(), nullable=True)
    op.add_column("financial_audit", sa.Column("settlement_evidence_id", sa.UUID()))
    op.add_column("financial_audit", sa.Column("financial_exception_id", sa.UUID()))
    op.create_foreign_key(
        "fk_audit_settlement",
        "financial_audit",
        "settlement_evidence",
        ["settlement_evidence_id"],
        ["settlement_evidence_id"],
    )
    op.create_foreign_key(
        "fk_audit_exception",
        "financial_audit",
        "financial_exception",
        ["financial_exception_id"],
        ["financial_exception_id"],
    )
    op.create_check_constraint(
        "ck_financial_audit_target",
        "financial_audit",
        "payment_id IS NOT NULL OR evidence_id IS NOT NULL OR settlement_evidence_id IS NOT NULL",
    )
    op.add_column(
        "financial_scan_checkpoint", sa.Column("last_checked_at", sa.DateTime(timezone=True))
    )
    op.add_column("financial_scan_checkpoint", sa.Column("last_error_code", sa.String(64)))


def downgrade() -> None:
    # Unmatched audits cannot be discarded or assigned a fictitious Payment.
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM financial_audit WHERE payment_id IS NULL
        OR settlement_evidence_id IS NOT NULL OR financial_exception_id IS NOT NULL)
        THEN RAISE EXCEPTION 'Unmatched audits require forward recovery'; END IF; END $$""")
    op.drop_column("financial_scan_checkpoint", "last_error_code")
    op.drop_column("financial_scan_checkpoint", "last_checked_at")
    op.drop_constraint("ck_financial_audit_target", "financial_audit", type_="check")
    op.drop_constraint("fk_audit_exception", "financial_audit", type_="foreignkey")
    op.drop_constraint("fk_audit_settlement", "financial_audit", type_="foreignkey")
    op.drop_column("financial_audit", "financial_exception_id")
    op.drop_column("financial_audit", "settlement_evidence_id")
    op.alter_column("financial_audit", "payment_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("financial_exception", "last_escalated_at")
    op.drop_column("financial_exception", "detected_at")
    op.drop_column("financial_exception", "occurrence_count")
