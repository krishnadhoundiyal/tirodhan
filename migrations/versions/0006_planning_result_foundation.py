"""Add planning result persistence foundation.

Revision ID: 0006_planning_result
Revises: 0005_planning_freeze
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_planning_result"
down_revision: str | None = "0005_planning_freeze"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_group",
        sa.Column("collection_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("planning_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("planning_mode", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["planning_batch_id"],
            ["planning_batch.planning_batch_id"],
            name="fk_collection_group_planning_batch",
        ),
        sa.PrimaryKeyConstraint("collection_group_id", name="pk_collection_group"),
    )
    op.create_table(
        "collection_group_member",
        sa.Column("collection_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["collection_group_id"],
            ["collection_group.collection_group_id"],
            name="fk_collection_group_member_group",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["collection_request.request_id"],
            name="fk_collection_group_member_request",
        ),
        sa.PrimaryKeyConstraint(
            "collection_group_id", "request_id", name="pk_collection_group_member"
        ),
        sa.UniqueConstraint("request_id", name="uq_collection_group_member_request"),
    )
    op.create_table(
        "pickup_execution",
        sa.Column("pickup_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["collection_group_id"],
            ["collection_group.collection_group_id"],
            name="fk_pickup_execution_group",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["collection_request.request_id"],
            name="fk_pickup_execution_request",
        ),
        sa.PrimaryKeyConstraint("pickup_execution_id", name="pk_pickup_execution"),
        sa.UniqueConstraint("request_id", name="uq_pickup_execution_request"),
    )


def downgrade() -> None:
    op.drop_table("pickup_execution")
    op.drop_table("collection_group_member")
    op.drop_table("collection_group")
