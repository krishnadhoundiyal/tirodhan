"""refund_lifecycle

Revision ID: 0016
Revises: 0015_authentication_challenge
Create Date: 2024-03-22 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015_authentication_challenge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create refund table
    op.create_table(
        "refund",
        sa.Column("refund_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_refund_id", sa.String(length=200), nullable=True),
        sa.Column("provider_idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["payment_attempt_id"],
            ["payment_attempt.payment_attempt_id"],
            name=op.f("fk_refund_payment_attempt_id_payment_attempt"),
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payment.payment_id"], name=op.f("fk_refund_payment_id_payment")
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["app_user.user_id"],
            name=op.f("fk_refund_requested_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("refund_id", name=op.f("pk_refund")),
        sa.CheckConstraint("amount_minor > 0", name="ck_refund_amount_positive"),
        sa.UniqueConstraint("provider", "provider_idempotency_key", name="uq_refund_provider_key"),
    )

    op.create_index(
        "uq_refund_provider_refund",
        "refund",
        ["provider", "provider_refund_id"],
        unique=True,
        postgresql_where=sa.text("provider_refund_id IS NOT NULL"),
    )

    # Add refund_id to payment_provider_event
    op.add_column(
        "payment_provider_event",
        sa.Column("refund_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_payment_provider_event_refund_id_refund"),
        "payment_provider_event",
        "refund",
        ["refund_id"],
        ["refund_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_payment_provider_event_refund_id_refund"),
        "payment_provider_event",
        type_="foreignkey",
    )
    op.drop_column("payment_provider_event", "refund_id")
    op.drop_index("uq_refund_provider_refund", table_name="refund")
    op.drop_table("refund")
