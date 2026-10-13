"""Durable V2 operational inputs and non-authoritative evaluation runs."""

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_routing_operational_inputs"
down_revision = "0023_payment_checkout_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operating_vehicle",
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("mass_capacity_g", sa.BigInteger(), nullable=False),
        sa.Column("volume_capacity_ml", sa.BigInteger(), nullable=False),
        sa.Column("profile_version", sa.String(length=100), nullable=False),
        sa.Column("end_receiving_point_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("recorded_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mass_capacity_g > 0 AND volume_capacity_ml > 0", name="ck_vehicle_capacity"
        ),
        sa.CheckConstraint("version > 0", name="ck_vehicle_version"),
        sa.ForeignKeyConstraint(
            ["end_receiving_point_id"],
            ["receiving_point.receiving_point_id"],
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by"],
            ["app_user.user_id"],
        ),
        sa.PrimaryKeyConstraint("vehicle_id"),
    )
    op.create_table(
        "routing_receiving_window",
        sa.Column("window_id", sa.Uuid(), nullable=False),
        sa.Column("receiving_point_id", sa.UUID(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ends_at > starts_at", name="ck_routing_receiving_window"),
        sa.ForeignKeyConstraint(
            ["receiving_point_id"],
            ["receiving_point.receiving_point_id"],
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by"],
            ["app_user.user_id"],
        ),
        sa.PrimaryKeyConstraint("window_id"),
    )
    op.create_table(
        "routing_shadow_run",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=False),
        sa.Column("client_command_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("configuration", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("problem", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_fingerprint", sa.LargeBinary(), nullable=True),
        sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'RUNNING' AND lease_token IS NULL AND lease_until IS NULL)",
            name="ck_shadow_lease",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'STALE')",
            name="ck_shadow_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_shadow_attempt"),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["app_user.user_id"],
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("requested_by", "client_command_id", name="uq_routing_shadow_command"),
    )
    op.create_table(
        "operating_shift",
        sa.Column("shift_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("rider_id", sa.UUID(), nullable=False),
        sa.Column("resource_kind", sa.String(length=16), nullable=False),
        sa.Column("commitment_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("resource_kind IN ('COMMITTED', 'FLEXIBLE')", name="ck_shift_kind"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_shift_window"),
        sa.ForeignKeyConstraint(
            ["recorded_by"],
            ["app_user.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["rider_id"],
            ["rider_profile.rider_id"],
        ),
        sa.ForeignKeyConstraint(
            ["vehicle_id"],
            ["operating_vehicle.vehicle_id"],
        ),
        sa.PrimaryKeyConstraint("shift_id"),
    )
    op.create_index(
        "uq_open_rider_shift",
        "operating_shift",
        ["rider_id"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index(
        "uq_open_vehicle_shift",
        "operating_shift",
        ["vehicle_id"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_table(
        "vehicle_position",
        sa.Column("position_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "location",
            geoalchemy2.types.Geography(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeogFromText",
                name="geography",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_position_revision"),
        sa.ForeignKeyConstraint(
            ["recorded_by"],
            ["app_user.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["vehicle_id"],
            ["operating_vehicle.vehicle_id"],
        ),
        sa.PrimaryKeyConstraint("position_id"),
        sa.UniqueConstraint("vehicle_id", "revision", name="uq_vehicle_position_revision"),
    )
    op.create_table(
        "routing_manifest",
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("mass_g", sa.BigInteger(), nullable=False),
        sa.Column("volume_ml", sa.BigInteger(), nullable=False),
        sa.Column("pickup_seconds", sa.Integer(), nullable=False),
        sa.Column("discharge_seconds", sa.Integer(), nullable=False),
        sa.Column("discharge_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receiving_point_id", sa.UUID(), nullable=False),
        sa.Column("recorded_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mass_g > 0 AND volume_ml > 0", name="ck_manifest_load"),
        sa.CheckConstraint(
            "pickup_seconds >= 0 AND discharge_seconds >= 0", name="ck_manifest_service"
        ),
        sa.ForeignKeyConstraint(
            ["receiving_point_id"],
            ["receiving_point.receiving_point_id"],
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by"],
            ["app_user.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["collection_request.request_id"],
        ),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_table(
        "cargo_vehicle_observation",
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("pickup_attempt_id", sa.UUID(), nullable=False),
        sa.Column("recorded_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["pickup_attempt_id"],
            ["pickup_attempt.pickup_attempt_id"],
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by"],
            ["app_user.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["collection_request.request_id"],
        ),
        sa.ForeignKeyConstraint(
            ["vehicle_id"],
            ["operating_vehicle.vehicle_id"],
        ),
        sa.PrimaryKeyConstraint("request_id"),
    )


def downgrade() -> None:
    op.drop_table("cargo_vehicle_observation")
    op.drop_table("routing_manifest")
    op.drop_table("vehicle_position")
    op.drop_table("operating_shift")
    op.drop_table("routing_shadow_run")
    op.drop_table("routing_receiving_window")
    op.drop_table("operating_vehicle")
