"""0017_fleet_dispatch_notification

Revision ID: 0017_fleet_dispatch_notification
Revises: 0016_refund_lifecycle
Create Date: 2026-10-02 16:07:33.308037
"""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = '0017_fleet_dispatch_notification'
down_revision: str | None = '0016_refund_lifecycle'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "fleet",
        sa.Column("fleet_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fleet_id"),
        sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="ck_fleet_status"),
    )
    op.create_table(
        "fleet_membership",
        sa.Column("fleet_membership_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fleet_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rider_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fleet_membership_id"),
        sa.ForeignKeyConstraint(["fleet_id"], ["fleet.fleet_id"]),
        sa.ForeignKeyConstraint(["rider_id"], ["rider_profile.rider_id"]),
    )
    op.create_index("uq_fleet_membership_active_rider", "fleet_membership", ["rider_id"], unique=True, postgresql_where=sa.text("left_at IS NULL"))
    op.create_table(
        "fleet_service_cell",
        sa.Column("fleet_service_cell_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fleet_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cell_id", sa.String(length=20), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fleet_service_cell_id"),
        sa.ForeignKeyConstraint(["fleet_id"], ["fleet.fleet_id"]),
    )
    op.create_index("uq_fleet_service_cell_active", "fleet_service_cell", ["fleet_id", "cell_id"], unique=True, postgresql_where=sa.text("deactivated_at IS NULL"))
    op.create_table(
        "rider_service_cell",
        sa.Column("rider_service_cell_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rider_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cell_id", sa.String(length=20), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("rider_service_cell_id"),
        sa.ForeignKeyConstraint(["rider_id"], ["rider_profile.rider_id"]),
    )
    op.create_index("uq_rider_service_cell_active", "rider_service_cell", ["rider_id", "cell_id"], unique=True, postgresql_where=sa.text("deactivated_at IS NULL"))
    op.create_table(
        "push_registration",
        sa.Column("push_registration_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rider_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_device_id", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("platform", sa.String(length=24), nullable=False),
        sa.Column("registration_token", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("push_registration_id"),
        sa.ForeignKeyConstraint(["rider_id"], ["rider_profile.rider_id"]),
    )
    op.add_column("assignment_offer", sa.Column("audience_kind", sa.String(length=24), server_default="INDEPENDENT", nullable=False))
    op.add_column("assignment_offer", sa.Column("fleet_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_assignment_offer_fleet_id", "assignment_offer", "fleet", ["fleet_id"], ["fleet_id"])
    op.create_check_constraint("ck_assignment_offer_audience_kind", "assignment_offer", "audience_kind IN ('FLEET', 'INDEPENDENT')")
    op.create_check_constraint("ck_assignment_offer_audience_fleet", "assignment_offer", "(audience_kind = 'FLEET' AND fleet_id IS NOT NULL) OR (audience_kind = 'INDEPENDENT' AND fleet_id IS NULL)")
    op.create_table(
        "offer_notification_delivery",
        sa.Column("offer_notification_delivery_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("offer_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("push_registration_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("offer_notification_delivery_id"),
        sa.ForeignKeyConstraint(["offer_id"], ["assignment_offer.offer_id"]),
        sa.ForeignKeyConstraint(["push_registration_id"], ["push_registration.push_registration_id"]),
        sa.UniqueConstraint("offer_id", "push_registration_id", name="uq_offer_notification_delivery"),
    )

def downgrade() -> None:
    op.drop_table("offer_notification_delivery")
    op.drop_constraint("ck_assignment_offer_audience_fleet", "assignment_offer", type_="check")
    op.drop_constraint("ck_assignment_offer_audience_kind", "assignment_offer", type_="check")
    op.drop_constraint("fk_assignment_offer_fleet_id", "assignment_offer", type_="foreignkey")
    op.drop_column("assignment_offer", "fleet_id")
    op.drop_column("assignment_offer", "audience_kind")
    op.drop_table("push_registration")
    op.drop_table("rider_service_cell")
    op.drop_table("fleet_service_cell")
    op.drop_table("fleet_membership")
    op.drop_table("fleet")
