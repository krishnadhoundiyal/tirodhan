"""Add the identity root and reliability substrate.

Revision ID: 0002_domain_reliability
Revises: 0001_enable_postgis
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_domain_reliability"
down_revision: str | None = "0001_enable_postgis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="pk_app_user"),
    )

    op.create_table(
        "idempotency_record",
        sa.Column(
            "idempotency_record_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("scope", sa.String(length=120), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("result_resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "idempotency_record_id",
            name="pk_idempotency_record",
        ),
        sa.UniqueConstraint(
            "scope",
            "idempotency_key",
            name="uq_idempotency_record_scope_key",
        ),
    )

    op.create_table(
        "inbox_message",
        sa.Column("consumer_name", sa.String(length=100), nullable=False),
        sa.Column("message_id", sa.String(length=200), nullable=False),
        sa.Column("message_type", sa.String(length=100), nullable=False),
        sa.Column("business_key", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "consumer_name",
            "message_id",
            name="pk_inbox_message",
        ),
    )

    op.create_table(
        "outbox_event",
        sa.Column("outbox_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_key", sa.String(length=200), nullable=False),
        sa.Column("aggregate_type", sa.String(length=80), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "publish_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("outbox_event_id", name="pk_outbox_event"),
        sa.UniqueConstraint("event_key", name="uq_outbox_event_event_key"),
    )


def downgrade() -> None:
    op.drop_table("outbox_event")
    op.drop_table("inbox_message")
    op.drop_table("idempotency_record")
    op.drop_table("app_user")
