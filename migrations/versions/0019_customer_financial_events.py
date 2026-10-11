"""Retain minimal authenticated capture facts for financial reconciliation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_customer_financial_events"
down_revision: str | None = "0018_customer_mobile_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing events remain valid; no guessed historical capture facts/backfill.
    op.add_column("payment_provider_event", sa.Column("provider_payment_id", sa.String(200)))
    op.add_column("payment_provider_event", sa.Column("amount_minor", sa.BigInteger()))
    op.add_column("payment_provider_event", sa.Column("currency", sa.CHAR(3)))
    op.create_check_constraint(
        "ck_provider_event_amount",
        "payment_provider_event",
        "amount_minor IS NULL OR amount_minor > 0",
    )


def downgrade() -> None:
    # Explicit downgrade removes only the new metadata, not financial/event rows.
    op.drop_constraint("ck_provider_event_amount", "payment_provider_event", type_="check")
    op.drop_column("payment_provider_event", "currency")
    op.drop_column("payment_provider_event", "amount_minor")
    op.drop_column("payment_provider_event", "provider_payment_id")
