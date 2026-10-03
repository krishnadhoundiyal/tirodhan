"""Fleet coverage, historical offer audiences and durable push deliveries."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0017_fleet_dispatch_notification"
down_revision: str | None = "0016_refund_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fleet",
        sa.Column("fleet_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="ck_fleet_status"),
    )
    op.create_table(
        "fleet_membership",
        sa.Column("fleet_membership_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fleet_id", UUID(as_uuid=True), sa.ForeignKey("fleet.fleet_id"), nullable=False),
        sa.Column(
            "rider_id", UUID(as_uuid=True), sa.ForeignKey("rider_profile.rider_id"), nullable=False
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_fleet_membership_current_rider",
        "fleet_membership",
        ["rider_id"],
        unique=True,
        postgresql_where=sa.text("left_at IS NULL"),
    )
    for table, owner, target in (
        ("fleet_service_cell", "fleet_id", "fleet.fleet_id"),
        ("rider_service_cell", "rider_id", "rider_profile.rider_id"),
    ):
        op.create_table(
            table,
            sa.Column(f"{table}_id", UUID(as_uuid=True), primary_key=True),
            sa.Column(owner, UUID(as_uuid=True), sa.ForeignKey(target), nullable=False),
            sa.Column("cell_id", sa.String(200), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deactivated_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            f"uq_{table}_active",
            table,
            [owner, "cell_id"],
            unique=True,
            postgresql_where=sa.text("deactivated_at IS NULL"),
        )
    op.create_table(
        "push_registration",
        sa.Column("push_registration_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "rider_id", UUID(as_uuid=True), sa.ForeignKey("rider_profile.rider_id"), nullable=False
        ),
        sa.Column("client_device_id", sa.String(200), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("registration_token", sa.String(4096), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("provider = 'FCM'", name="ck_push_registration_provider"),
        sa.CheckConstraint("platform IN ('ANDROID', 'IOS')", name="ck_push_registration_platform"),
    )
    op.create_index(
        "uq_push_registration_active_device",
        "push_registration",
        ["rider_id", "client_device_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    # Before fleets existed every historical offer was non-fleet. Keep conventional
    # independent default for the unchanged manual offer-creation operation as well.
    op.add_column(
        "assignment_offer",
        sa.Column("audience_kind", sa.String(16), nullable=False, server_default="INDEPENDENT"),
    )
    op.add_column("assignment_offer", sa.Column("fleet_id", UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_assignment_offer_fleet", "assignment_offer", "fleet", ["fleet_id"], ["fleet_id"]
    )
    op.create_check_constraint(
        "ck_assignment_offer_audience",
        "assignment_offer",
        "(audience_kind = 'FLEET' AND fleet_id IS NOT NULL) OR "
        "(audience_kind = 'INDEPENDENT' AND fleet_id IS NULL)",
    )
    op.create_table(
        "offer_notification_delivery",
        sa.Column("offer_notification_delivery_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "offer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("assignment_offer.offer_id"),
            nullable=False,
        ),
        sa.Column(
            "push_registration_id",
            UUID(as_uuid=True),
            sa.ForeignKey("push_registration.push_registration_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("provider_message_id", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "offer_id", "push_registration_id", name="uq_offer_notification_delivery_device"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SENT', 'PERMANENTLY_FAILED')",
            name="ck_offer_notification_delivery_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("offer_notification_delivery")
    op.drop_constraint("ck_assignment_offer_audience", "assignment_offer", type_="check")
    op.drop_constraint("fk_assignment_offer_fleet", "assignment_offer", type_="foreignkey")
    op.drop_column("assignment_offer", "fleet_id")
    op.drop_column("assignment_offer", "audience_kind")
    for table in (
        "push_registration",
        "rider_service_cell",
        "fleet_service_cell",
        "fleet_membership",
        "fleet",
    ):
        op.drop_table(table)
