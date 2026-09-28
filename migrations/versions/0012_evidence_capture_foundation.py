"""Add immutable evidence captures and business-target links.

Revision ID: 0012_evidence_capture_foundation
Revises: 0011_receiving_point_handover
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_evidence_capture_foundation"
down_revision: str | None = "0011_receiving_point_handover"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_capture",
        sa.Column("evidence_capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("captured_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["captured_by_user_id"], ["app_user.user_id"]),
        sa.PrimaryKeyConstraint("evidence_capture_id"),
        sa.UniqueConstraint(
            "client_capture_id",
            name="uq_evidence_capture_client_capture",
        ),
    )
    op.create_table(
        "pickup_evidence_link",
        sa.Column("pickup_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["pickup_execution_id"],
            ["pickup_execution.pickup_execution_id"],
        ),
        sa.ForeignKeyConstraint(
            ["evidence_capture_id"],
            ["evidence_capture.evidence_capture_id"],
        ),
        sa.PrimaryKeyConstraint("pickup_execution_id", "evidence_capture_id"),
        sa.UniqueConstraint(
            "evidence_capture_id",
            name="uq_pickup_evidence_link_capture",
        ),
    )
    op.create_table(
        "handover_evidence_link",
        sa.Column("handover_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["handover_event_id"],
            ["handover_event.handover_event_id"],
        ),
        sa.ForeignKeyConstraint(
            ["evidence_capture_id"],
            ["evidence_capture.evidence_capture_id"],
        ),
        sa.PrimaryKeyConstraint("handover_event_id", "evidence_capture_id"),
        sa.UniqueConstraint(
            "evidence_capture_id",
            name="uq_handover_evidence_link_capture",
        ),
    )


def downgrade() -> None:
    op.drop_table("pickup_evidence_link")
    op.drop_table("handover_evidence_link")
    op.drop_table("evidence_capture")
