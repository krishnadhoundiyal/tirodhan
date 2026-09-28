"""Add receiving points and immutable handover history.

Revision ID: 0011_receiving_point_handover
Revises: 0010_pickup_incident_reassign
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision: str = "0011_receiving_point_handover"
down_revision: str | None = "0010_pickup_incident_reassign"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receiving_point",
        sa.Column("receiving_point_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("official_name", sa.String(length=200), nullable=False),
        sa.Column("official_identifier", sa.String(length=200), nullable=True),
        sa.Column(
            "location",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("allowed_radius_m", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')",
            name="ck_receiving_point_status",
        ),
        sa.CheckConstraint(
            "allowed_radius_m > 0",
            name="ck_receiving_point_positive_allowed_radius",
        ),
        sa.CheckConstraint("version > 0", name="ck_receiving_point_positive_version"),
        sa.PrimaryKeyConstraint("receiving_point_id"),
    )
    op.create_index(
        "ix_receiving_point_location_gist",
        "receiving_point",
        ["location"],
        postgresql_using="gist",
    )
    op.create_table(
        "handover_event",
        sa.Column("handover_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_handover_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receiving_point_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "observed_location",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "receiving_point_location_snapshot",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("allowed_radius_m_snapshot", sa.Integer(), nullable=False),
        sa.Column("distance_m", sa.Float(precision=53), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("validation_code", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('VALIDATED', 'REJECTED')",
            name="ck_handover_event_status",
        ),
        sa.CheckConstraint(
            "validation_code IN ('WITHIN_ALLOWED_RADIUS', 'OUTSIDE_ALLOWED_RADIUS')",
            name="ck_handover_event_validation_code",
        ),
        sa.CheckConstraint(
            "(status = 'VALIDATED' AND validation_code = 'WITHIN_ALLOWED_RADIUS') OR "
            "(status = 'REJECTED' AND validation_code = 'OUTSIDE_ALLOWED_RADIUS')",
            name="ck_handover_event_validation_consistency",
        ),
        sa.CheckConstraint(
            "allowed_radius_m_snapshot > 0",
            name="ck_handover_event_positive_radius_snapshot",
        ),
        sa.CheckConstraint(
            "distance_m >= 0",
            name="ck_handover_event_nonnegative_distance",
        ),
        sa.ForeignKeyConstraint(["rider_id"], ["rider_profile.rider_id"]),
        sa.ForeignKeyConstraint(
            ["receiving_point_id"],
            ["receiving_point.receiving_point_id"],
        ),
        sa.PrimaryKeyConstraint("handover_event_id"),
        sa.UniqueConstraint(
            "client_handover_id",
            name="uq_handover_event_client_handover",
        ),
    )
    op.create_table(
        "handover_event_item",
        sa.Column("handover_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pickup_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('VALIDATED', 'REJECTED')",
            name="ck_handover_event_item_status",
        ),
        sa.ForeignKeyConstraint(
            ["handover_event_id"],
            ["handover_event.handover_event_id"],
        ),
        sa.ForeignKeyConstraint(
            ["pickup_execution_id"],
            ["pickup_execution.pickup_execution_id"],
        ),
        sa.PrimaryKeyConstraint("handover_event_id", "pickup_execution_id"),
    )
    op.create_index(
        "uq_handover_event_item_validated_pickup",
        "handover_event_item",
        ["pickup_execution_id"],
        unique=True,
        postgresql_where=sa.text("status = 'VALIDATED'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_handover_event_item_validated_pickup",
        table_name="handover_event_item",
    )
    op.drop_table("handover_event_item")
    op.drop_table("handover_event")
    op.drop_index("ix_receiving_point_location_gist", table_name="receiving_point")
    op.drop_table("receiving_point")
