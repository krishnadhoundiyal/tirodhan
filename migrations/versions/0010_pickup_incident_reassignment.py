"""Add pickup incidents and residual-work reassignment state.

Revision ID: 0010_pickup_incident_reassign
Revises: 0009_pickup_execution_lifecycle
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_pickup_incident_reassign"
down_revision: str | None = "0009_pickup_execution_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rider_assignment",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("ck_rider_assignment_status", "rider_assignment", type_="check")
    op.create_check_constraint(
        "ck_rider_assignment_status",
        "rider_assignment",
        "status IN ('ACTIVE', 'COMPLETED', 'SUPERSEDED')",
    )
    op.create_check_constraint(
        "ck_rider_assignment_item_release_reason",
        "rider_assignment_item",
        "release_reason_code IS NULL OR release_reason_code = 'REASSIGNED'",
    )
    op.create_check_constraint(
        "ck_rider_assignment_item_release_consistency",
        "rider_assignment_item",
        "released_at IS NOT NULL OR release_reason_code IS NULL",
    )
    op.create_table(
        "pickup_incident",
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pickup_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rider_assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("resolution_code", sa.String(length=32), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reason_code IN ('CUSTOMER_UNAVAILABLE', 'ADDRESS_NOT_FOUND', "
            "'ACCESS_BLOCKED', 'RIDER_UNABLE_TO_REACH', "
            "'RIDER_UNABLE_TO_CONTINUE', 'OTHER')",
            name="ck_pickup_incident_reason",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'RESOLVED')",
            name="ck_pickup_incident_status",
        ),
        sa.CheckConstraint(
            "(status = 'OPEN' AND resolution_code IS NULL AND resolved_at IS NULL "
            "AND resolved_by_user_id IS NULL) OR "
            "(status = 'RESOLVED' AND resolution_code = 'REASSIGNED' "
            "AND resolved_at IS NOT NULL AND resolved_by_user_id IS NOT NULL)",
            name="ck_pickup_incident_lifecycle",
        ),
        sa.ForeignKeyConstraint(["pickup_execution_id"], ["pickup_execution.pickup_execution_id"]),
        sa.ForeignKeyConstraint(["rider_assignment_id"], ["rider_assignment.assignment_id"]),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["app_user.user_id"]),
        sa.PrimaryKeyConstraint("incident_id"),
        sa.UniqueConstraint(
            "client_incident_id",
            name="uq_pickup_incident_client_incident",
        ),
    )


def downgrade() -> None:
    op.drop_table("pickup_incident")
    op.drop_constraint(
        "ck_rider_assignment_item_release_consistency",
        "rider_assignment_item",
        type_="check",
    )
    op.drop_constraint(
        "ck_rider_assignment_item_release_reason",
        "rider_assignment_item",
        type_="check",
    )
    op.drop_constraint("ck_rider_assignment_status", "rider_assignment", type_="check")
    op.execute(
        sa.text(
            "UPDATE rider_assignment "
            "SET completed_at = COALESCE(completed_at, superseded_at), status = 'COMPLETED' "
            "WHERE status = 'SUPERSEDED'"
        )
    )
    op.drop_column("rider_assignment", "superseded_at")
    op.create_check_constraint(
        "ck_rider_assignment_status",
        "rider_assignment",
        "status IN ('ACTIVE', 'COMPLETED')",
    )
