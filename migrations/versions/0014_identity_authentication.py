"""Add verified phone identity, roles, and stable refresh sessions.

Revision ID: 0014_identity_authentication
Revises: 0013_media_asset_foundation
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_identity_authentication"
down_revision: str | None = "0013_media_asset_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_phone",
        sa.Column("user_phone_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("phone_lookup_hmac", sa.LargeBinary(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.user_id"]),
        sa.PrimaryKeyConstraint("user_phone_id"),
    )
    op.create_index(
        "uq_user_phone_active_lookup_hmac",
        "user_phone",
        ["phone_lookup_hmac"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_index(
        "uq_user_phone_active_user",
        "user_phone",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )

    op.create_table(
        "user_role",
        sa.Column("user_role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_code", sa.String(length=24), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "role_code IN ('CUSTOMER', 'RIDER', 'MANAGER')",
            name="ck_user_role_code",
        ),
        sa.ForeignKeyConstraint(["granted_by_user_id"], ["app_user.user_id"]),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["app_user.user_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.user_id"]),
        sa.PrimaryKeyConstraint("user_role_id"),
    )
    op.create_index(
        "uq_user_role_active_user_code",
        "user_role",
        ["user_id", "role_code"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "refresh_session",
        sa.Column("refresh_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("expires_at > created_at", name="ck_refresh_session_expiry"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.user_id"]),
        sa.PrimaryKeyConstraint("refresh_session_id"),
        sa.UniqueConstraint("credential_hash", name="uq_refresh_session_credential_hash"),
    )


def downgrade() -> None:
    op.drop_table("refresh_session")
    op.drop_index("uq_user_role_active_user_code", table_name="user_role")
    op.drop_table("user_role")
    op.drop_index("uq_user_phone_active_user", table_name="user_phone")
    op.drop_index("uq_user_phone_active_lookup_hmac", table_name="user_phone")
    op.drop_table("user_phone")
