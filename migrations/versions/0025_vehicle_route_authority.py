"""Transactional route authority and vehicle-bound execution; no activation."""

import sqlalchemy as sa
from alembic import op

revision = "0025_vehicle_route_authority"
down_revision = "0024_routing_operational_inputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("pickup_execution", "collection_group_id", nullable=True)
    op.alter_column("rider_assignment", "collection_group_id", nullable=True)
    op.drop_constraint("ck_rider_assignment_source", "rider_assignment", type_="check")
    op.drop_constraint("ck_rider_assignment_source_audit", "rider_assignment", type_="check")
    op.create_check_constraint(
        "ck_rider_assignment_source",
        "rider_assignment",
        "source IN ('RIDER_OFFER_ACCEPTED', 'MANAGER_ASSIGNED', 'COMMITTED_DISPATCH')",
    )
    op.create_check_constraint(
        "ck_rider_assignment_source_audit",
        "rider_assignment",
        "(source IN ('MANAGER_ASSIGNED', 'COMMITTED_DISPATCH') "
        "AND assigned_by_user_id IS NOT NULL) OR "
        "(source = 'RIDER_OFFER_ACCEPTED' AND assigned_by_user_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_rider_assignment_group_source",
        "rider_assignment",
        "(source = 'COMMITTED_DISPATCH' AND collection_group_id IS NULL) OR "
        "(source <> 'COMMITTED_DISPATCH' AND collection_group_id IS NOT NULL)",
    )
    op.drop_constraint(
        "ck_rider_assignment_item_release_reason", "rider_assignment_item", type_="check"
    )
    op.create_check_constraint(
        "ck_rider_assignment_item_release_reason",
        "rider_assignment_item",
        "release_reason_code IS NULL OR release_reason_code IN ('REASSIGNED', 'CANCELLED')",
    )
    op.create_table(
        "route_activation",
        sa.Column("activation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("engine", sa.String(length=16), nullable=False),
        sa.Column("activated_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("engine IN ('VROOM', 'OR_TOOLS')", name="ck_route_activation_engine"),
        sa.ForeignKeyConstraint(
            ["activated_by"],
            ["app_user.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["routing_shadow_run.run_id"],
        ),
        sa.PrimaryKeyConstraint("activation_id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_table(
        "vehicle_route",
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_id", sa.Uuid(), nullable=False),
        sa.Column("shift_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'RECOVERY_REQUIRED', 'CLOSED')", name="ck_vehicle_route_status"
        ),
        sa.CheckConstraint("version > 0", name="ck_vehicle_route_version"),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["rider_assignment.assignment_id"],
        ),
        sa.ForeignKeyConstraint(
            ["shift_id"],
            ["operating_shift.shift_id"],
        ),
        sa.ForeignKeyConstraint(
            ["vehicle_id"],
            ["operating_vehicle.vehicle_id"],
        ),
        sa.PrimaryKeyConstraint("route_id"),
        sa.UniqueConstraint("assignment_id"),
    )
    op.create_table(
        "route_recovery",
        sa.Column("recovery_id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("manager_id", sa.UUID(), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reason_code IN ('VEHICLE_UNAVAILABLE', 'RIDER_UNAVAILABLE', 'EXECUTION_EXCEPTION')",
            name="ck_route_recovery_reason",
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"],
            ["app_user.user_id"],
        ),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["vehicle_route.route_id"],
        ),
        sa.PrimaryKeyConstraint("recovery_id"),
    )
    op.create_table(
        "route_revision",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("activation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("supersedes_revision_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_route_revision_version"),
        sa.ForeignKeyConstraint(
            ["activation_id"],
            ["route_activation.activation_id"],
        ),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["vehicle_route.route_id"],
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_revision_id"],
            ["route_revision.revision_id"],
        ),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint("route_id", "version", name="uq_route_revision_version"),
    )
    op.create_table(
        "route_task",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("stop_id", sa.Uuid(), nullable=False),
        sa.Column("pickup_execution_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("receiving_point_id", sa.UUID(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(kind = 'PICKUP' AND receiving_point_id IS NULL) OR "
            "(kind = 'DISCHARGE' AND receiving_point_id IS NOT NULL)",
            name="ck_route_task_destination",
        ),
        sa.CheckConstraint("kind IN ('PICKUP', 'DISCHARGE')", name="ck_route_task_kind"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'STARTED', 'COMPLETED', 'CANCELLED')", name="ck_route_task_state"
        ),
        sa.ForeignKeyConstraint(
            ["pickup_execution_id"],
            ["pickup_execution.pickup_execution_id"],
        ),
        sa.ForeignKeyConstraint(
            ["receiving_point_id"],
            ["receiving_point.receiving_point_id"],
        ),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["vehicle_route.route_id"],
        ),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_table(
        "route_discharge_proof",
        sa.Column("handover_event_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_capture_id", sa.UUID(), nullable=False),
        sa.Column("receiving_identifier_snapshot", sa.String(length=200), nullable=False),
        sa.Column("evidence_kind", sa.String(length=16), nullable=False),
        sa.CheckConstraint("evidence_kind = 'PHOTO'", name="ck_route_discharge_photo"),
        sa.ForeignKeyConstraint(
            ["evidence_capture_id"],
            ["evidence_capture.evidence_capture_id"],
        ),
        sa.ForeignKeyConstraint(
            ["handover_event_id"],
            ["handover_event.handover_event_id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["route_task.task_id"],
        ),
        sa.PrimaryKeyConstraint("handover_event_id"),
        sa.UniqueConstraint("evidence_capture_id"),
    )
    op.create_table(
        "route_revision_stop",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0 AND planned_end >= planned_start", name="ck_route_revision_stop_schedule"
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["route_revision.revision_id"],
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["route_task.task_id"],
        ),
        sa.PrimaryKeyConstraint("revision_id", "ordinal"),
        sa.UniqueConstraint("revision_id", "task_id", name="uq_route_revision_task"),
    )

    op.create_index(
        "uq_vehicle_route_open_vehicle",
        "vehicle_route",
        ["vehicle_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'CLOSED'"),
    )
    op.create_index(
        "uq_vehicle_route_open_shift",
        "vehicle_route",
        ["shift_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'CLOSED'"),
    )
    op.create_index(
        "uq_route_task_live_stop",
        "route_task",
        ["route_id", "stop_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('PENDING', 'STARTED')"),
    )
    op.execute("""
        CREATE FUNCTION reject_route_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'route history is append-only' USING ERRCODE = '23514'; END $$;
    """)
    for table in (
        "route_activation",
        "route_revision",
        "route_revision_stop",
        "route_recovery",
        "route_discharge_proof",
    ):
        op.execute(
            f"CREATE TRIGGER route_history_immutable BEFORE UPDATE OR DELETE ON "
            f"{table} FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()"
        )

    op.create_check_constraint(
        "ck_vehicle_route_closed_at",
        "vehicle_route",
        "(status = 'CLOSED') = (closed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_route_task_timestamps",
        "route_task",
        "(state = 'PENDING' AND started_at IS NULL AND completed_at IS NULL) OR "
        "(state = 'STARTED' AND started_at IS NOT NULL AND completed_at IS NULL) OR "
        "(state = 'COMPLETED' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND completed_at >= started_at) OR (state = 'CANCELLED' AND completed_at IS NULL)",
    )
    op.execute("""
      CREATE FUNCTION enforce_vehicle_route_binding() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'UPDATE' AND
          ROW(NEW.route_id, NEW.vehicle_id, NEW.shift_id, NEW.assignment_id) IS DISTINCT FROM
          ROW(OLD.route_id, OLD.vehicle_id, OLD.shift_id, OLD.assignment_id) THEN
          RAISE EXCEPTION 'route binding is immutable' USING ERRCODE = '23514';
        END IF;
        IF NOT EXISTS (
          SELECT 1 FROM operating_shift s JOIN rider_assignment a ON a.rider_id = s.rider_id
          WHERE s.shift_id = NEW.shift_id AND s.vehicle_id = NEW.vehicle_id
          AND s.resource_kind = 'COMMITTED' AND a.assignment_id = NEW.assignment_id
          AND a.source = 'COMMITTED_DISPATCH'
        ) THEN RAISE EXCEPTION 'invalid vehicle/shift/assignment binding' USING ERRCODE = '23514';
        END IF;
        IF TG_OP = 'UPDATE' AND OLD.status = 'CLOSED' AND NEW IS DISTINCT FROM OLD THEN
          RAISE EXCEPTION 'closed route is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
      END $$;
    """)
    op.execute("""
      CREATE FUNCTION enforce_route_task_transition() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF ROW(NEW.task_id, NEW.route_id, NEW.stop_id, NEW.pickup_execution_id,
               NEW.kind, NEW.receiving_point_id) IS DISTINCT FROM
           ROW(OLD.task_id, OLD.route_id, OLD.stop_id, OLD.pickup_execution_id,
               OLD.kind, OLD.receiving_point_id) OR
           (OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at) OR
           (OLD.state IN ('COMPLETED', 'CANCELLED') AND NEW IS DISTINCT FROM OLD) OR
           (OLD.state = 'PENDING' AND NEW.state NOT IN ('PENDING', 'STARTED', 'CANCELLED')) OR
           (OLD.state = 'STARTED' AND NEW.state NOT IN ('STARTED', 'COMPLETED', 'CANCELLED')) THEN
          RAISE EXCEPTION 'invalid or historical route task mutation' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
      END $$;
    """)
    op.execute("""
      CREATE FUNCTION enforce_operating_shift_identity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN
          RAISE EXCEPTION 'shift history is retained' USING ERRCODE = '23514';
        END IF;
        IF ROW(NEW.shift_id, NEW.vehicle_id, NEW.rider_id, NEW.resource_kind,
               NEW.commitment_id, NEW.starts_at, NEW.ends_at) IS DISTINCT FROM
           ROW(OLD.shift_id, OLD.vehicle_id, OLD.rider_id, OLD.resource_kind,
               OLD.commitment_id, OLD.starts_at, OLD.ends_at) OR
           (OLD.closed_at IS NOT NULL AND NEW.closed_at IS DISTINCT FROM OLD.closed_at) THEN
          RAISE EXCEPTION 'shift identity is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
      END $$;
    """)
    op.execute("""
      CREATE TRIGGER vehicle_route_binding BEFORE INSERT OR UPDATE ON vehicle_route
      FOR EACH ROW EXECUTE FUNCTION enforce_vehicle_route_binding()
    """)
    op.execute("""
      CREATE TRIGGER route_task_transition BEFORE UPDATE ON route_task
      FOR EACH ROW EXECUTE FUNCTION enforce_route_task_transition()
    """)
    op.execute("""
      CREATE TRIGGER operating_shift_identity BEFORE UPDATE OR DELETE ON operating_shift
      FOR EACH ROW EXECUTE FUNCTION enforce_operating_shift_identity()
    """)


def downgrade() -> None:
    # Operational history cannot be converted into fictitious legacy groups.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM route_activation) THEN
            RAISE EXCEPTION '0025 downgrade blocked by V2 history; disable runtime instead';
          END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER vehicle_route_binding ON vehicle_route")
    op.execute("DROP TRIGGER route_task_transition ON route_task")
    op.execute("DROP TRIGGER operating_shift_identity ON operating_shift")
    op.execute("DROP FUNCTION enforce_vehicle_route_binding()")
    op.execute("DROP FUNCTION enforce_route_task_transition()")
    op.execute("DROP FUNCTION enforce_operating_shift_identity()")
    for table in (
        "route_activation",
        "route_revision",
        "route_revision_stop",
        "route_recovery",
        "route_discharge_proof",
    ):
        op.execute(f"DROP TRIGGER route_history_immutable ON {table}")
    op.execute("DROP FUNCTION reject_route_history_mutation()")
    op.drop_table("route_revision_stop")
    op.drop_table("route_discharge_proof")
    op.drop_table("route_task")
    op.drop_table("route_revision")
    op.drop_table("route_recovery")
    op.drop_table("vehicle_route")
    op.drop_table("route_activation")
    op.drop_constraint("ck_rider_assignment_group_source", "rider_assignment", type_="check")
    op.drop_constraint("ck_rider_assignment_source", "rider_assignment", type_="check")
    op.drop_constraint("ck_rider_assignment_source_audit", "rider_assignment", type_="check")
    op.create_check_constraint(
        "ck_rider_assignment_source",
        "rider_assignment",
        "source IN ('RIDER_OFFER_ACCEPTED', 'MANAGER_ASSIGNED')",
    )
    op.create_check_constraint(
        "ck_rider_assignment_source_audit",
        "rider_assignment",
        "(source = 'MANAGER_ASSIGNED' AND assigned_by_user_id IS NOT NULL) OR "
        "(source = 'RIDER_OFFER_ACCEPTED' AND assigned_by_user_id IS NULL)",
    )
    op.drop_constraint(
        "ck_rider_assignment_item_release_reason", "rider_assignment_item", type_="check"
    )
    op.create_check_constraint(
        "ck_rider_assignment_item_release_reason",
        "rider_assignment_item",
        "release_reason_code IS NULL OR release_reason_code = 'REASSIGNED'",
    )
    op.alter_column("rider_assignment", "collection_group_id", nullable=False)
    op.alter_column("pickup_execution", "collection_group_id", nullable=False)
