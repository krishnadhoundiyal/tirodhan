"""Add deterministic compaction policy snapshots and planner indexes.

Revision ID: 0007_compaction_planner
Revises: 0006_planning_result
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_compaction_planner"
down_revision: str | None = "0006_planning_result"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "planning_batch",
        sa.Column("compaction_distance_m_snapshot", sa.Integer(), nullable=True),
    )
    op.add_column(
        "planning_batch",
        sa.Column("max_group_requests_snapshot", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_planning_batch_positive_compaction_distance",
        "planning_batch",
        "compaction_distance_m_snapshot IS NULL OR compaction_distance_m_snapshot > 0",
    )
    op.create_check_constraint(
        "ck_planning_batch_positive_max_group_requests",
        "planning_batch",
        "max_group_requests_snapshot IS NULL OR max_group_requests_snapshot > 0",
    )
    op.create_index(
        "ix_collection_request_planning_batch_id",
        "collection_request",
        ["planning_batch_id"],
    )
    op.create_index(
        "ix_collection_request_pickup_location_gist",
        "collection_request",
        ["pickup_location"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_collection_request_pickup_location_gist",
        table_name="collection_request",
    )
    op.drop_index(
        "ix_collection_request_planning_batch_id",
        table_name="collection_request",
    )
    op.drop_constraint(
        "ck_planning_batch_positive_max_group_requests",
        "planning_batch",
        type_="check",
    )
    op.drop_constraint(
        "ck_planning_batch_positive_compaction_distance",
        "planning_batch",
        type_="check",
    )
    op.drop_column("planning_batch", "max_group_requests_snapshot")
    op.drop_column("planning_batch", "compaction_distance_m_snapshot")
