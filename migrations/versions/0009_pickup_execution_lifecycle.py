"""Add pickup execution lifecycle and immutable attempts.

Revision ID: 0009_pickup_execution_lifecycle
Revises: 0008_rider_dispatch_assignment
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_pickup_execution_lifecycle"
down_revision: str | None = "0008_rider_dispatch_assignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_rider_assignment_status", "rider_assignment", type_="check")
    op.create_check_constraint(
        "ck_rider_assignment_status",
        "rider_assignment",
        "status IN ('ACTIVE', 'COMPLETED')",
    )
    op.create_check_constraint(
        "ck_pickup_execution_status",
        "pickup_execution",
        "status IN ('PENDING_ASSIGNMENT', 'ASSIGNED', 'COLLECTED')",
    )
    op.create_table(
        "pickup_attempt",
        sa.Column("pickup_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pickup_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rider_assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_number > 0", name="ck_pickup_attempt_positive_number"),
        sa.CheckConstraint(
            "outcome IN ('COLLECTED', 'NOT_COLLECTED')",
            name="ck_pickup_attempt_outcome",
        ),
        sa.ForeignKeyConstraint(["pickup_execution_id"], ["pickup_execution.pickup_execution_id"]),
        sa.ForeignKeyConstraint(["rider_assignment_id"], ["rider_assignment.assignment_id"]),
        sa.PrimaryKeyConstraint("pickup_attempt_id"),
        sa.UniqueConstraint(
            "pickup_execution_id",
            "client_attempt_id",
            name="uq_pickup_attempt_client_attempt",
        ),
        sa.UniqueConstraint(
            "pickup_execution_id",
            "attempt_number",
            name="uq_pickup_attempt_number",
        ),
    )


def downgrade() -> None:
    op.drop_table("pickup_attempt")
    op.drop_constraint("ck_pickup_execution_status", "pickup_execution", type_="check")
    op.drop_constraint("ck_rider_assignment_status", "rider_assignment", type_="check")
    op.create_check_constraint(
        "ck_rider_assignment_status",
        "rider_assignment",
        "status = 'ACTIVE'",
    )
