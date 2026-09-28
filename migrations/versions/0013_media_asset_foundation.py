"""Add original-media storage lifecycle metadata.

Revision ID: 0013_media_asset_foundation
Revises: 0012_evidence_capture_foundation
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_media_asset_foundation"
down_revision: str | None = "0012_evidence_capture_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_asset",
        sa.Column("media_asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_media_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("object_key", sa.String(length=80), nullable=False),
        sa.Column("expected_content_type", sa.String(length=255), nullable=False),
        sa.Column("stored_content_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("upload_status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "media_type IN ('PHOTO', 'VIDEO')",
            name="ck_media_asset_media_type",
        ),
        sa.CheckConstraint(
            "upload_status IN ('PENDING_UPLOAD', 'FINALIZED')",
            name="ck_media_asset_upload_status",
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_media_asset_nonnegative_size",
        ),
        sa.CheckConstraint(
            "(upload_status = 'PENDING_UPLOAD' "
            "AND stored_content_type IS NULL "
            "AND size_bytes IS NULL "
            "AND finalized_at IS NULL) "
            "OR (upload_status = 'FINALIZED' "
            "AND stored_content_type IS NOT NULL "
            "AND size_bytes IS NOT NULL "
            "AND finalized_at IS NOT NULL)",
            name="ck_media_asset_lifecycle_consistency",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_capture_id"],
            ["evidence_capture.evidence_capture_id"],
        ),
        sa.PrimaryKeyConstraint("media_asset_id"),
        sa.UniqueConstraint("client_media_id", name="uq_media_asset_client_media"),
        sa.UniqueConstraint(
            "evidence_capture_id",
            name="uq_media_asset_evidence_capture",
        ),
        sa.UniqueConstraint("object_key", name="uq_media_asset_object_key"),
    )


def downgrade() -> None:
    op.drop_table("media_asset")
