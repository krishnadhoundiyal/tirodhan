"""Continuous committed fleet control, telemetry provenance and protected departure."""

import sqlalchemy as sa
from alembic import op

revision = "0026_continuous_fleet"
down_revision = "0025_vehicle_route_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operating_vehicle", sa.Column("blocked_reason", sa.String(24), nullable=True))
    op.add_column("vehicle_position", sa.Column("accuracy_m", sa.Integer(), nullable=True))
    op.add_column("vehicle_position", sa.Column("session_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_position_session",
        "vehicle_position",
        "refresh_session",
        ["session_id"],
        ["refresh_session_id"],
    )
    op.add_column(
        "route_task", sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "routing_shadow_run", sa.Column("business_fingerprint", sa.LargeBinary(), nullable=True)
    )
    op.execute(
        """
CREATE TABLE fleet_control (
	scope VARCHAR(32) NOT NULL,
	manager_id UUID NOT NULL,
	version BIGINT NOT NULL,
	configuration JSONB NOT NULL,
	lease_token UUID,
	lease_until TIMESTAMP WITH TIME ZONE,
	next_due_at TIMESTAMP WITH TIME ZONE NOT NULL,
	business_fingerprint BYTEA,
	last_evaluated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (scope),
	CONSTRAINT ck_fleet_scope CHECK (scope = 'COMMITTED_FLEET'),
	CONSTRAINT ck_fleet_version CHECK (version > 0),
	CONSTRAINT ck_fleet_lease CHECK ((lease_token IS NULL) = (lease_until IS NULL)),
	FOREIGN KEY(manager_id) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        """
CREATE TABLE fleet_cycle (
	cycle_id UUID NOT NULL,
	scope VARCHAR(32) NOT NULL,
	control_version BIGINT NOT NULL,
	requested_by UUID NOT NULL,
	lease_token UUID NOT NULL,
	status VARCHAR(24) NOT NULL,
	run_id UUID,
	activation_id UUID,
	selected_engine VARCHAR(16),
	measurements JSONB,
	failure_code VARCHAR(100),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (cycle_id),
	CONSTRAINT ck_fleet_cycle_status CHECK (status IN
            ('RUNNING','ACTIVATED','UNCHANGED','FAILED','STALE')),
	FOREIGN KEY(scope) REFERENCES fleet_control (scope),
	FOREIGN KEY(requested_by) REFERENCES app_user (user_id),
	UNIQUE (lease_token),
	FOREIGN KEY(run_id) REFERENCES routing_shadow_run (run_id),
	FOREIGN KEY(activation_id) REFERENCES route_activation (activation_id)
)
        """
    )
    op.execute("CREATE INDEX ix_fleet_cycle_created ON fleet_cycle (created_at)")
    op.execute(
        """
CREATE TABLE fleet_exception (
	exception_id UUID NOT NULL,
	cycle_id UUID,
	request_id UUID,
	route_id UUID,
	reported_by UUID NOT NULL,
	reason VARCHAR(100) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (exception_id),
	CONSTRAINT ck_fleet_exception_target CHECK
            (cycle_id IS NOT NULL OR request_id IS NOT NULL OR route_id IS NOT NULL),
	FOREIGN KEY(cycle_id) REFERENCES fleet_cycle (cycle_id),
	FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
	FOREIGN KEY(route_id) REFERENCES vehicle_route (route_id),
	FOREIGN KEY(reported_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute("CREATE INDEX ix_fleet_exception_created ON fleet_exception (created_at)")
    op.execute(
        """
CREATE TABLE fleet_position_anchor (
	vehicle_id UUID NOT NULL,
	scope VARCHAR(32) NOT NULL,
	position_id UUID NOT NULL,
	PRIMARY KEY (vehicle_id),
	FOREIGN KEY(vehicle_id) REFERENCES operating_vehicle (vehicle_id),
	FOREIGN KEY(scope) REFERENCES fleet_control (scope),
	FOREIGN KEY(position_id) REFERENCES vehicle_position (position_id)
)
        """
    )
    op.execute(
        """
CREATE TABLE routing_facility_restriction (
	restriction_id UUID NOT NULL,
	receiving_point_id UUID NOT NULL,
	reason VARCHAR(24) NOT NULL,
	starts_at TIMESTAMP WITH TIME ZONE NOT NULL,
	ends_at TIMESTAMP WITH TIME ZONE NOT NULL,
	recorded_by UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (restriction_id),
	CONSTRAINT ck_facility_restriction_window CHECK (ends_at > starts_at),
	CONSTRAINT ck_facility_restriction_reason CHECK (reason IN ('CLOSED','FULL','UNAVAILABLE')),
	FOREIGN KEY(receiving_point_id) REFERENCES receiving_point (receiving_point_id),
	FOREIGN KEY(recorded_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        "CREATE INDEX ix_facility_restriction_point ON routing_facility_restriction "
        "(receiving_point_id, starts_at, ends_at)"
    )
    op.execute("""
      CREATE FUNCTION guard_route_commitment() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF (OLD.committed_at IS NOT NULL AND NEW.committed_at IS DISTINCT FROM OLD.committed_at)
           OR (OLD.committed_at IS NULL AND NEW.committed_at IS NOT NULL
               AND NEW.state NOT IN ('PENDING','STARTED')) THEN
          RAISE EXCEPTION 'route departure is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
      END $$;
    """)
    op.execute("""
      CREATE TRIGGER route_commitment BEFORE UPDATE ON route_task
      FOR EACH ROW EXECUTE FUNCTION guard_route_commitment();
    """)
    for table in (
        "vehicle_position",
        "routing_manifest",
        "fleet_exception",
        "routing_facility_restriction",
    ):
        op.execute(
            f"CREATE TRIGGER fleet_history_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()"
        )

    op.create_check_constraint(
        "ck_vehicle_block_reason",
        "operating_vehicle",
        "blocked_reason IS NULL OR blocked_reason IN "
        "('BREAKDOWN','LOAD_UNCERTAIN','DISCHARGE_DELAY','SAFETY')",
    )
    op.create_check_constraint(
        "ck_position_accuracy",
        "vehicle_position",
        "accuracy_m IS NULL OR accuracy_m BETWEEN 0 AND 1000",
    )
    op.create_check_constraint(
        "ck_position_provenance", "vehicle_position", "session_id IS NULL OR accuracy_m IS NOT NULL"
    )
    op.create_check_constraint(
        "ck_task_commitment_time",
        "route_task",
        "committed_at IS NULL OR started_at IS NULL OR committed_at <= started_at",
    )
    op.create_check_constraint(
        "ck_fleet_selected_engine",
        "fleet_cycle",
        "selected_engine IS NULL OR selected_engine IN ('VROOM','OR_TOOLS')",
    )
    op.create_check_constraint(
        "ck_fleet_cycle_completion", "fleet_cycle", "(status = 'RUNNING') = (completed_at IS NULL)"
    )
    op.create_check_constraint(
        "ck_fleet_cycle_authority",
        "fleet_cycle",
        "status <> 'ACTIVATED' OR (activation_id IS NOT NULL AND selected_engine IS NOT NULL)",
    )
    op.execute("""
      CREATE FUNCTION guard_fleet_cycle_history() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN
          RAISE EXCEPTION 'fleet cycle history is retained' USING ERRCODE = '23514';
        END IF;
        IF ROW(NEW.cycle_id, NEW.scope, NEW.requested_by, NEW.control_version, NEW.lease_token,
               NEW.created_at) IS DISTINCT FROM
           ROW(OLD.cycle_id, OLD.scope, OLD.requested_by, OLD.control_version, OLD.lease_token,
               OLD.created_at) THEN
          RAISE EXCEPTION 'fleet cycle identity is immutable' USING ERRCODE = '23514';
        END IF;
        IF OLD.status <> 'RUNNING' AND NEW IS DISTINCT FROM OLD THEN
          RAISE EXCEPTION 'terminal fleet cycle is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
      END $$;
    """)
    op.execute(
        "CREATE TRIGGER fleet_cycle_history BEFORE UPDATE OR DELETE ON fleet_cycle "
        "FOR EACH ROW EXECUTE FUNCTION guard_fleet_cycle_history()"
    )


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM fleet_control) OR EXISTS (SELECT 1 FROM fleet_cycle)
           OR EXISTS (SELECT 1 FROM fleet_exception)
           OR EXISTS (SELECT 1 FROM routing_facility_restriction)
           OR EXISTS (SELECT 1 FROM route_task WHERE committed_at IS NOT NULL)
           OR EXISTS (SELECT 1 FROM vehicle_position WHERE session_id IS NOT NULL) THEN
          RAISE EXCEPTION '0026 downgrade requires preservation of fleet operational history';
        END IF;
      END $$;
    """)
    for table in (
        "vehicle_position",
        "routing_manifest",
        "fleet_exception",
        "routing_facility_restriction",
    ):
        op.execute(f"DROP TRIGGER fleet_history_immutable ON {table}")
    op.execute("DROP TRIGGER route_commitment ON route_task")
    op.execute("DROP FUNCTION guard_route_commitment()")
    op.execute("DROP TRIGGER fleet_cycle_history ON fleet_cycle")
    op.execute("DROP FUNCTION guard_fleet_cycle_history()")
    for table in (
        "fleet_position_anchor",
        "fleet_exception",
        "fleet_cycle",
        "fleet_control",
        "routing_facility_restriction",
    ):
        op.drop_table(table)
    op.drop_column("routing_shadow_run", "business_fingerprint")
    op.drop_constraint("ck_task_commitment_time", "route_task", type_="check")
    op.drop_column("route_task", "committed_at")
    op.drop_constraint("fk_position_session", "vehicle_position", type_="foreignkey")
    op.drop_constraint("ck_position_accuracy", "vehicle_position", type_="check")
    op.drop_constraint("ck_position_provenance", "vehicle_position", type_="check")
    op.drop_column("vehicle_position", "session_id")
    op.drop_column("vehicle_position", "accuracy_m")
    op.drop_constraint("ck_vehicle_block_reason", "operating_vehicle", type_="check")
    op.drop_column("operating_vehicle", "blocked_reason")
