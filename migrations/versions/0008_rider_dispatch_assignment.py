"""Add rider dispatch and initial assignment foundation.

Revision ID: 0008_rider_dispatch_assignment
Revises: 0007_compaction_planner
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_rider_dispatch_assignment"
down_revision: str | None = "0007_compaction_planner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_pickup_execution_collection_group_id",
        "pickup_execution",
        ["collection_group_id"],
    )
    op.create_table(
        "rider_profile",
        sa.Column("rider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("vehicle_type_code", sa.String(length=64), nullable=True),
        sa.Column("capacity_class_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED')", name="ck_rider_profile_status"),
        sa.ForeignKeyConstraint(["rider_id"], ["app_user.user_id"]),
        sa.PrimaryKeyConstraint("rider_id"),
    )
    op.create_table(
        "rider_availability",
        sa.Column("rider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("availability_intent", sa.String(length=16), nullable=False),
        sa.Column("work_state", sa.String(length=16), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "availability_intent IN ('OFFLINE', 'AVAILABLE')",
            name="ck_rider_availability_intent",
        ),
        sa.CheckConstraint(
            "work_state IN ('IDLE', 'RESERVED', 'BUSY')",
            name="ck_rider_availability_work_state",
        ),
        sa.CheckConstraint("version > 0", name="ck_rider_availability_positive_version"),
        sa.ForeignKeyConstraint(["rider_id"], ["rider_profile.rider_id"]),
        sa.PrimaryKeyConstraint("rider_id"),
    )
    op.create_table(
        "assignment_offer",
        sa.Column("offer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("offer_round", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("offered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("offer_round > 0", name="ck_assignment_offer_positive_round"),
        sa.CheckConstraint("expires_at > offered_at", name="ck_assignment_offer_valid_expiry"),
        sa.CheckConstraint(
            "status IN ('OPEN', 'ACCEPTED', 'CLOSED_LOST')",
            name="ck_assignment_offer_status",
        ),
        sa.ForeignKeyConstraint(["collection_group_id"], ["collection_group.collection_group_id"]),
        sa.ForeignKeyConstraint(["rider_id"], ["rider_profile.rider_id"]),
        sa.PrimaryKeyConstraint("offer_id"),
        sa.UniqueConstraint(
            "collection_group_id",
            "rider_id",
            "offer_round",
            name="uq_assignment_offer_business_key",
        ),
    )
    op.create_table(
        "rider_assignment",
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("assigned_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("supersedes_assignment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status = 'ACTIVE'", name="ck_rider_assignment_status"),
        sa.CheckConstraint(
            "source IN ('RIDER_OFFER_ACCEPTED', 'MANAGER_ASSIGNED')",
            name="ck_rider_assignment_source",
        ),
        sa.CheckConstraint(
            "(source = 'MANAGER_ASSIGNED' AND assigned_by_user_id IS NOT NULL) OR "
            "(source = 'RIDER_OFFER_ACCEPTED' AND assigned_by_user_id IS NULL)",
            name="ck_rider_assignment_source_audit",
        ),
        sa.ForeignKeyConstraint(["assigned_by_user_id"], ["app_user.user_id"]),
        sa.ForeignKeyConstraint(["collection_group_id"], ["collection_group.collection_group_id"]),
        sa.ForeignKeyConstraint(["rider_id"], ["rider_profile.rider_id"]),
        sa.ForeignKeyConstraint(["supersedes_assignment_id"], ["rider_assignment.assignment_id"]),
        sa.PrimaryKeyConstraint("assignment_id"),
    )
    op.create_index(
        "uq_rider_assignment_active_group",
        "rider_assignment",
        ["collection_group_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "rider_assignment_item",
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pickup_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["assignment_id"], ["rider_assignment.assignment_id"]),
        sa.ForeignKeyConstraint(["pickup_execution_id"], ["pickup_execution.pickup_execution_id"]),
        sa.PrimaryKeyConstraint("assignment_id", "pickup_execution_id"),
    )
    op.create_index(
        "uq_rider_assignment_item_active_pickup",
        "rider_assignment_item",
        ["pickup_execution_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_rider_assignment_item_active_pickup", table_name="rider_assignment_item")
    op.drop_table("rider_assignment_item")
    op.drop_index("uq_rider_assignment_active_group", table_name="rider_assignment")
    op.drop_table("rider_assignment")
    op.drop_table("assignment_offer")
    op.drop_table("rider_availability")
    op.drop_table("rider_profile")
    op.drop_index("ix_pickup_execution_collection_group_id", table_name="pickup_execution")
