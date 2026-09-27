"""Add planning batch attempt foundation.

Revision ID: 0005_planning_freeze
Revises: 0004_request_payment
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_planning_freeze"
down_revision: str | None = "0004_request_payment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planning_batch_attempt",
        sa.Column(
            "planning_batch_attempt_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("planning_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["planning_batch_id"],
            ["planning_batch.planning_batch_id"],
            name="fk_planning_batch_attempt_batch",
        ),
        sa.PrimaryKeyConstraint(
            "planning_batch_attempt_id",
            name="pk_planning_batch_attempt",
        ),
        sa.UniqueConstraint(
            "planning_batch_id",
            "attempt_number",
            name="uq_planning_batch_attempt_number",
        ),
    )


def downgrade() -> None:
    op.drop_table("planning_batch_attempt")
