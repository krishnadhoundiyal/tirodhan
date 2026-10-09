"""Unseeded catalogue presentation metadata and customer read query indexes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0018_customer_mobile_core"
down_revision: str | None = "0017_fleet_dispatch_notification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalogue_media",
        sa.Column("media_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("object_key", sa.String(300), nullable=False, unique=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("alt_text", sa.String(300), nullable=False),
        sa.Column("blurhash", sa.String(100)),
        sa.CheckConstraint("width > 0 AND height > 0", name="ck_catalogue_media_dimensions"),
        sa.CheckConstraint(
            "object_key LIKE 'product-art/%'", name="ck_catalogue_media_product_key"
        ),
    )
    op.create_table(
        "catalogue_group",
        sa.Column("group_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("group_code", sa.String(100), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "catalogue_category",
        sa.Column("category_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("category_code", sa.String(100), nullable=False, unique=True),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("catalogue_group.group_id"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("image_id", UUID(as_uuid=True), sa.ForeignKey("catalogue_media.media_id")),
        sa.Column("thumbnail_id", UUID(as_uuid=True), sa.ForeignKey("catalogue_media.media_id")),
        sa.Column("handling_hints", sa.String(2000)),
        sa.Column("quantity_input", sa.String(16), nullable=False),
        sa.Column("weight_input", sa.String(16), nullable=False),
        sa.Column("quick_label", sa.String(200)),
        sa.Column("quick_order", sa.Integer()),
        sa.CheckConstraint(
            "quantity_input IN ('NONE', 'OPTIONAL')", name="ck_category_quantity_input"
        ),
        sa.CheckConstraint("weight_input IN ('NONE', 'OPTIONAL')", name="ck_category_weight_input"),
    )
    op.create_table(
        "catalogue_artwork",
        sa.Column("artwork_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False, unique=True),
        sa.Column(
            "media_id",
            UUID(as_uuid=True),
            sa.ForeignKey("catalogue_media.media_id"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('hero', 'home', 'rickshaw', 'receiving_point')",
            name="ck_catalogue_artwork_kind",
        ),
    )
    op.add_column("collection_request_item", sa.Column("display_name_snapshot", sa.String(200)))
    op.create_index(
        "ix_customer_collection_page",
        "collection_request",
        ["customer_id", sa.text("created_at DESC"), sa.text("request_id DESC")],
    )
    op.create_index("ix_collection_item_request", "collection_request_item", ["request_id"])
    op.create_index(
        "ix_payment_attempt_payment_created", "payment_attempt", ["payment_id", "created_at"]
    )
    op.create_index("ix_refund_payment_created", "refund", ["payment_id", "created_at"])
    op.create_index("ix_handover_item_pickup", "handover_event_item", ["pickup_execution_id"])


def downgrade() -> None:
    for name, table in (
        ("ix_handover_item_pickup", "handover_event_item"),
        ("ix_refund_payment_created", "refund"),
        ("ix_payment_attempt_payment_created", "payment_attempt"),
        ("ix_collection_item_request", "collection_request_item"),
        ("ix_customer_collection_page", "collection_request"),
    ):
        op.drop_index(name, table_name=table)
    op.drop_column("collection_request_item", "display_name_snapshot")
    for table in ("catalogue_artwork", "catalogue_category", "catalogue_group", "catalogue_media"):
        op.drop_table(table)
