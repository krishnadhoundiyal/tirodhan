"""Persist transaction-bound authentication intent without storing OTP credentials."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_authentication_challenge"
down_revision: str | None = "0014_identity_authentication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "authentication_challenge",
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("phone_lookup_hmac", sa.LargeBinary(), nullable=False),
        sa.Column("provider_code", sa.String(32), nullable=False),
        sa.Column("provider_reference", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("challenge_id"),
        sa.UniqueConstraint("client_request_id", name="uq_authentication_challenge_client_request"),
        sa.UniqueConstraint(
            "provider_code", "provider_reference", name="uq_authentication_challenge_provider"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_authentication_challenge_expiry"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'CONSUMED', 'SUPERSEDED')",
            name="ck_authentication_challenge_status",
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND consumed_at IS NULL AND superseded_at IS NULL) OR "
            "(status = 'CONSUMED' AND consumed_at IS NOT NULL AND superseded_at IS NULL) OR "
            "(status = 'SUPERSEDED' AND consumed_at IS NULL AND superseded_at IS NOT NULL)",
            name="ck_authentication_challenge_timestamps",
        ),
    )
    op.create_index(
        "uq_authentication_challenge_active_phone",
        "authentication_challenge",
        ["phone_lookup_hmac"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index("uq_authentication_challenge_active_phone", table_name="authentication_challenge")
    op.drop_table("authentication_challenge")
