"""Complete rider incidents and scoped recovery activation provenance."""

from alembic import op

revision = "0035_rider_failure_recovery"
down_revision = "0034_replacement_options"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE rider_failure_incident (
        incident_id uuid PRIMARY KEY, rider_id uuid NOT NULL REFERENCES rider_profile,
        region_id uuid NOT NULL REFERENCES operating_region, reported_by uuid NOT NULL
        REFERENCES app_user,
        reason varchar(100) NOT NULL, status varchar(16) NOT NULL, version bigint NOT NULL,
        created_at timestamptz NOT NULL,
        CONSTRAINT ck_failure_incident_state CHECK(status IN ('OPEN','RESOLVED') AND version >
        0))""")
    op.execute(
        "CREATE UNIQUE INDEX uq_failure_open_rider ON rider_failure_incident(rider_id) "
        "WHERE status='OPEN'"
    )
    op.execute("""CREATE TABLE rider_failure_route (
        incident_id uuid REFERENCES rider_failure_incident, route_id uuid REFERENCES vehicle_route,
        revision_id uuid NOT NULL REFERENCES route_revision, PRIMARY KEY(incident_id,route_id))""")
    op.execute("""CREATE TABLE recovery_assignment (
        assignment_id uuid PRIMARY KEY, incident_id uuid NOT NULL REFERENCES rider_failure_incident,
        candidate_id uuid NOT NULL REFERENCES replacement_candidate, shift_id uuid NOT NULL
        REFERENCES operating_shift,
        approved_by uuid NOT NULL REFERENCES app_user, approved_at timestamptz NOT NULL,
        status varchar(24) NOT NULL, version bigint NOT NULL, attempt bigint NOT NULL, due_at
        timestamptz NOT NULL,
        lease_token uuid, lease_until timestamptz, failure_code varchar(100), activation_id uuid
        REFERENCES route_activation,
        CONSTRAINT ck_recovery_assignment_state CHECK(status IN
        ('PENDING','RUNNING','AWAITING_TRANSFER','ACTIVATED','ATTENTION','SUPERSEDED')),
        CONSTRAINT ck_recovery_assignment_lease CHECK((lease_token IS NULL)=(lease_until IS
        NULL)))""")
    op.execute("CREATE INDEX ix_recovery_assignment_due_at ON recovery_assignment(due_at)")
    op.execute(
        "CREATE INDEX ix_recovery_incident ON recovery_assignment(incident_id,assignment_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_recovery_pending_shift ON recovery_assignment(shift_id) "
        "WHERE status IN ('PENDING','RUNNING','AWAITING_TRANSFER','ATTENTION')"
    )
    op.execute("""CREATE TABLE recovery_assignment_item (
        assignment_id uuid REFERENCES recovery_assignment, replacement_id uuid REFERENCES
        rider_replacement,
        released_at timestamptz, PRIMARY KEY(assignment_id,replacement_id))""")
    op.execute(
        "CREATE UNIQUE INDEX uq_recovery_item_active ON recovery_assignment_item(replacement_id) "
        "WHERE released_at IS NULL"
    )
    op.execute("""CREATE TABLE recovery_witness (
        witness_id uuid PRIMARY KEY, assignment_id uuid NOT NULL REFERENCES recovery_assignment,
        source_fingerprint bytea NOT NULL, configuration jsonb NOT NULL, problem jsonb NOT NULL,
        proposal jsonb NOT NULL, created_at timestamptz NOT NULL)""")
    op.execute("""CREATE TABLE route_push_delivery (
        revision_id uuid REFERENCES route_revision,
        registration_id uuid REFERENCES push_registration(push_registration_id),
        status varchar(24) NOT NULL, attempts bigint NOT NULL,
        PRIMARY KEY(revision_id,registration_id),
        CONSTRAINT ck_route_push_status CHECK(
        status IN ('PENDING','SENT','PERMANENTLY_FAILED') AND attempts >= 0))""")
    for table in ("rider_failure_route", "recovery_witness"):
        op.execute(
            f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()"
        )
    op.execute("""ALTER TABLE rider_replacement ADD COLUMN failure_incident_id uuid
        REFERENCES rider_failure_incident,
        ADD COLUMN recommendation_token uuid, ADD COLUMN recommendation_until timestamptz""")
    op.execute(
        "CREATE INDEX ix_replacement_failure_incident ON "
        "rider_replacement(failure_incident_id,replacement_id)"
    )
    op.execute("""ALTER TABLE route_activation ALTER COLUMN run_id DROP NOT NULL,
        ALTER COLUMN engine DROP NOT NULL, ADD COLUMN recovery_witness_id uuid UNIQUE REFERENCES
        recovery_witness,
        ADD CONSTRAINT ck_route_activation_origin CHECK(
        (run_id IS NOT NULL AND recovery_witness_id IS NULL AND engine IS NOT NULL) OR
        (run_id IS NULL AND recovery_witness_id IS NOT NULL AND engine IS NULL))""")

    op.execute("""CREATE FUNCTION guard_recovery_terms() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (NEW.incident_id,NEW.candidate_id,NEW.shift_id,NEW.approved_by,NEW.approved_at)
         IS DISTINCT FROM
         (OLD.incident_id,OLD.candidate_id,OLD.shift_id,OLD.approved_by,OLD.approved_at) THEN
        RAISE EXCEPTION 'recovery assignment terms are immutable' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER recovery_terms BEFORE UPDATE ON recovery_assignment "
        "FOR EACH ROW EXECUTE FUNCTION guard_recovery_terms()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM route_push_delivery)
        OR EXISTS(SELECT 1 FROM rider_failure_incident)
        OR EXISTS(SELECT 1 FROM recovery_assignment)
        OR EXISTS(SELECT 1 FROM recovery_witness) OR EXISTS(SELECT 1 FROM rider_replacement
        WHERE failure_incident_id IS NOT NULL)
        THEN RAISE EXCEPTION 'export adopted rider failure recovery before downgrade'; END IF;
        END $$""")
    op.execute("""ALTER TABLE route_activation DROP CONSTRAINT ck_route_activation_origin,
        DROP COLUMN recovery_witness_id, ALTER COLUMN run_id SET NOT NULL, ALTER COLUMN engine
        SET NOT NULL""")
    op.execute("DROP INDEX ix_replacement_failure_incident")
    op.execute(
        "ALTER TABLE rider_replacement DROP COLUMN failure_incident_id,"
        "DROP COLUMN recommendation_token,DROP COLUMN recommendation_until"
    )
    op.execute("DROP FUNCTION guard_recovery_terms() CASCADE")
    op.execute(
        "DROP TABLE route_push_delivery,recovery_witness,recovery_assignment_item,"
        "recovery_assignment,rider_failure_route,rider_failure_incident"
    )
