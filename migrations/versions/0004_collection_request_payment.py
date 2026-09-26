"""Add collection request, payment, and planning serialization foundation.

Revision ID: 0004_request_payment
Revises: 0003_address_serviceability
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision: str = "0004_request_payment"
down_revision: str | None = "0003_address_serviceability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planning_batch",
        sa.Column("planning_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cell_id", sa.String(length=200), nullable=False),
        sa.Column("slot_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slot_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("completion_mode", sa.String(length=24), nullable=True),
        sa.Column("max_attempts_snapshot", sa.Integer(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("planning_batch_id", name="pk_planning_batch"),
        sa.UniqueConstraint(
            "cell_id", "slot_start", "slot_end", name="uq_planning_batch_work_unit"
        ),
    )

    op.create_table(
        "collection_request",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("serviceability_context_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pickup_address_snapshot_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "pickup_location",
            Geography(geometry_type="POINT", srid=4326),
            nullable=False,
        ),
        sa.Column("cell_id", sa.String(length=200), nullable=False),
        sa.Column("slot_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slot_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quoted_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payment_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planning_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "payment_expires_at > created_at", name="ck_collection_request_payment_expiry"
        ),
        sa.CheckConstraint(
            "quoted_amount_minor >= 0", name="ck_collection_request_nonnegative_quote"
        ),
        sa.CheckConstraint("slot_end > slot_start", name="ck_collection_request_slot_order"),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["app_user.user_id"], name="fk_collection_request_customer"
        ),
        sa.ForeignKeyConstraint(
            ["planning_batch_id"],
            ["planning_batch.planning_batch_id"],
            name="fk_collection_request_planning_batch",
        ),
        sa.ForeignKeyConstraint(
            ["serviceability_context_id"],
            ["serviceability_context.serviceability_context_id"],
            name="fk_collection_request_serviceability_context",
        ),
        sa.PrimaryKeyConstraint("request_id", name="pk_collection_request"),
        sa.UniqueConstraint(
            "customer_id", "client_request_id", name="uq_collection_request_customer_client"
        ),
        sa.UniqueConstraint(
            "serviceability_context_id", name="uq_collection_request_serviceability_context"
        ),
    )

    op.create_table(
        "collection_request_item",
        sa.Column("request_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_category_code", sa.String(length=100), nullable=False),
        sa.Column("declared_quantity", sa.Integer(), nullable=True),
        sa.Column("declared_weight_grams", sa.BigInteger(), nullable=True),
        sa.Column("quoted_line_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("pricing_rule_version", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["collection_request.request_id"],
            name="fk_collection_request_item_request",
        ),
        sa.PrimaryKeyConstraint("request_item_id", name="pk_collection_request_item"),
    )

    op.create_table(
        "payment",
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("successful_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["request_id"], ["collection_request.request_id"], name="fk_payment_request"
        ),
        sa.PrimaryKeyConstraint("payment_id", name="pk_payment"),
        sa.UniqueConstraint("request_id", name="uq_payment_request"),
    )

    op.create_table(
        "payment_attempt",
        sa.Column("payment_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_order_id", sa.String(length=200), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=200), nullable=True),
        sa.Column("provider_idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payment.payment_id"], name="fk_payment_attempt_payment"
        ),
        sa.PrimaryKeyConstraint("payment_attempt_id", name="pk_payment_attempt"),
        sa.UniqueConstraint(
            "provider", "provider_idempotency_key", name="uq_payment_attempt_provider_key"
        ),
    )
    op.create_index(
        "uq_payment_attempt_provider_order",
        "payment_attempt",
        ["provider", "provider_order_id"],
        unique=True,
        postgresql_where=sa.text("provider_order_id IS NOT NULL"),
    )
    op.create_index(
        "uq_payment_attempt_provider_payment",
        "payment_attempt",
        ["provider", "provider_payment_id"],
        unique=True,
        postgresql_where=sa.text("provider_payment_id IS NOT NULL"),
    )

    op.create_table(
        "payment_provider_event",
        sa.Column("payment_provider_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_event_id", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payment_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload_hash", sa.LargeBinary(), nullable=True),
        sa.Column("processing_status", sa.String(length=24), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["payment_attempt_id"],
            ["payment_attempt.payment_attempt_id"],
            name="fk_payment_provider_event_attempt",
        ),
        sa.PrimaryKeyConstraint("payment_provider_event_id", name="pk_payment_provider_event"),
        sa.UniqueConstraint(
            "provider", "external_event_id", name="uq_payment_provider_event_identity"
        ),
    )


def downgrade() -> None:
    op.drop_table("payment_provider_event")
    op.drop_index("uq_payment_attempt_provider_payment", table_name="payment_attempt")
    op.drop_index("uq_payment_attempt_provider_order", table_name="payment_attempt")
    op.drop_table("payment_attempt")
    op.drop_table("payment")
    op.drop_table("collection_request_item")
    op.drop_table("collection_request")
    op.drop_table("planning_batch")
