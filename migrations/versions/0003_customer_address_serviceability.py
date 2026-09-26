"""Add customer addresses and serviceability contexts.

Revision ID: 0003_address_serviceability
Revises: 0002_domain_reliability
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision: str = "0003_address_serviceability"
down_revision: str | None = "0002_domain_reliability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_address",
        sa.Column("address_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=True),
        sa.Column("address_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "location",
            Geography(geometry_type="POINT", srid=4326),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.user_id"], name="fk_user_address_user"),
        sa.PrimaryKeyConstraint("address_id", name="pk_user_address"),
    )
    op.create_index(
        "uq_user_address_one_active_default",
        "user_address",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true AND status = 'ACTIVE'"),
    )

    op.create_table(
        "serviceability_context",
        sa.Column("serviceability_context_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_address_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_address_version", sa.BigInteger(), nullable=True),
        sa.Column("address_snapshot_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "location",
            Geography(geometry_type="POINT", srid=4326),
            nullable=True,
        ),
        sa.Column("cell_id", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_address_id"],
            ["user_address.address_id"],
            name="fk_serviceability_context_source_address",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.user_id"], name="fk_serviceability_context_user"
        ),
        sa.PrimaryKeyConstraint("serviceability_context_id", name="pk_serviceability_context"),
    )


def downgrade() -> None:
    op.drop_table("serviceability_context")
    op.drop_index("uq_user_address_one_active_default", table_name="user_address")
    op.drop_table("user_address")
