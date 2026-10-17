"""Bounded durable retry scheduling for commercial capacity preparation."""

import sqlalchemy as sa
from alembic import op

revision = "0029_admission_retry_schedule"
down_revision = "0028_regional_parallel_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "capacity_admission_request",
        sa.Column(
            "region_id", sa.Uuid(), sa.ForeignKey("operating_region.region_id"), nullable=True
        ),
    )
    op.execute("""
        UPDATE capacity_admission_request a SET region_id = (
          SELECT rc.region_id FROM serviceability_context sc
          LEFT JOIN collection_request cr ON cr.request_id = a.reattempt_request_id
          JOIN operating_region_cell rc ON rc.cell_id = coalesce(cr.cell_id, sc.cell_id)
          WHERE sc.serviceability_context_id = a.context_id
        )
    """)
    # Drain existing admission workers before upgrade. Do not silently revoke live
    # owners if an old deployment has several RUNNING intents in the same region.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT region_id FROM capacity_admission_request
                   WHERE status='RUNNING' AND region_id IS NOT NULL
                   GROUP BY region_id HAVING count(*)>1) THEN
          RAISE EXCEPTION 'drain regional admission workers before migration 0029';
        END IF;
        END $$""")
    op.create_index(
        "uq_capacity_admission_region_running",
        "capacity_admission_request",
        ["region_id"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.add_column(
        "capacity_admission_request",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_capacity_admission_retry", "capacity_admission_request", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    # Clearing a retry delay only makes still-PENDING work immediately eligible.
    # Certificates, leases, command identity, financial and operational history survive.
    op.drop_index("ix_capacity_admission_retry", "capacity_admission_request")
    op.drop_column("capacity_admission_request", "next_attempt_at")
    op.drop_index("uq_capacity_admission_region_running", "capacity_admission_request")
    op.drop_column("capacity_admission_request", "region_id")
