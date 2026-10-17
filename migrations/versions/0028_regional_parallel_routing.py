"""Regional finite ownership, rider confirmation and durable per-booking outcomes."""

from alembic import op

revision = "0028_regional_parallel_routing"
down_revision = "0027_operational_orchestration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE receiving_point ADD COLUMN unloading_capacity INTEGER "
        "CONSTRAINT ck_receiving_unloading_capacity CHECK(unloading_capacity > 0)"
    )
    op.execute("ALTER TABLE fleet_control DROP CONSTRAINT ck_fleet_scope")
    op.execute("ALTER TABLE fleet_control ALTER COLUMN scope TYPE VARCHAR(64)")
    op.execute("ALTER TABLE fleet_cycle ALTER COLUMN scope TYPE VARCHAR(64)")
    op.execute("ALTER TABLE fleet_position_anchor ALTER COLUMN scope TYPE VARCHAR(64)")
    op.execute(
        "ALTER TABLE fleet_control ADD COLUMN region_id UUID UNIQUE "
        "REFERENCES operating_region(region_id)"
    )
    op.execute(
        "ALTER TABLE fleet_control ADD CONSTRAINT ck_fleet_scope CHECK "
        "((scope = 'COMMITTED_FLEET' AND region_id IS NULL) OR "
        "(region_id IS NOT NULL AND scope = 'REGION:' || region_id::text))"
    )
    for table, name, expression in (
        (
            "fleet_cycle",
            "ck_fleet_selected_engine",
            "selected_engine IS NULL OR selected_engine IN ('VROOM','OR_TOOLS','COMPOSITE')",
        ),
        (
            "route_activation",
            "ck_route_activation_engine",
            "engine IN ('VROOM','OR_TOOLS','COMPOSITE')",
        ),
    ):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})")
    op.execute("""CREATE TABLE routing_shift_confirmation (
        shift_id UUID PRIMARY KEY REFERENCES operating_shift(shift_id),
        rider_id UUID NOT NULL REFERENCES rider_profile(rider_id),
        confirmed_at TIMESTAMPTZ NOT NULL)""")
    op.execute("""CREATE TABLE regional_planning_outcome (
        cycle_id UUID NOT NULL REFERENCES fleet_cycle(cycle_id),
        request_id UUID NOT NULL REFERENCES collection_request(request_id),
        region_id UUID NOT NULL REFERENCES operating_region(region_id),
        generation BIGINT NOT NULL CONSTRAINT ck_planning_generation CHECK(generation >= 0),
        outcome VARCHAR(32) NOT NULL CONSTRAINT ck_planning_outcome
            CHECK(outcome IN ('ASSIGNED','EXISTING','DEFERRED','RECOVERY')),
        reason VARCHAR(100) NOT NULL, evaluated_at TIMESTAMPTZ NOT NULL, retry_at TIMESTAMPTZ,
        PRIMARY KEY(cycle_id,request_id))""")
    op.execute(
        "CREATE INDEX ix_planning_outcome_request ON "
        "regional_planning_outcome(request_id,evaluated_at)"
    )
    op.execute(
        "CREATE INDEX ix_planning_outcome_retry ON regional_planning_outcome(region_id,retry_at)"
    )
    op.execute("""CREATE FUNCTION protect_regional_history() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'regional planning/confirmation history is immutable'; END $$""")
    for table in ("regional_planning_outcome", "routing_shift_confirmation"):
        op.execute(
            f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION protect_regional_history()"
        )
    op.execute("""CREATE FUNCTION check_shift_confirmation() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE s operating_shift; BEGIN
        SELECT * INTO s FROM operating_shift WHERE shift_id=NEW.shift_id FOR NO KEY UPDATE;
        IF s.rider_id <> NEW.rider_id OR s.resource_kind <> 'COMMITTED' OR s.closed_at IS NOT NULL
           OR NEW.confirmed_at >= s.starts_at THEN
           RAISE EXCEPTION 'pre-shift rider confirmation required'; END IF;
        RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER check_shift_confirmation BEFORE INSERT ON "
        "routing_shift_confirmation FOR EACH ROW EXECUTE FUNCTION check_shift_confirmation()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM regional_planning_outcome)
        OR EXISTS(SELECT 1 FROM routing_shift_confirmation)
        OR EXISTS(SELECT 1 FROM fleet_control WHERE region_id IS NOT NULL)
        OR EXISTS(SELECT 1 FROM route_activation WHERE engine='COMPOSITE')
        OR EXISTS(SELECT 1 FROM receiving_point WHERE unloading_capacity IS NOT NULL)
        THEN RAISE EXCEPTION 'regional history requires export/drain before downgrade';
        END IF; END $$""")
    op.execute("DROP TABLE regional_planning_outcome, routing_shift_confirmation")
    op.execute("ALTER TABLE receiving_point DROP COLUMN unloading_capacity")
    op.execute("DROP FUNCTION protect_regional_history(), check_shift_confirmation()")
    for table, name, expression in (
        (
            "fleet_cycle",
            "ck_fleet_selected_engine",
            "selected_engine IS NULL OR selected_engine IN ('VROOM','OR_TOOLS')",
        ),
        ("route_activation", "ck_route_activation_engine", "engine IN ('VROOM','OR_TOOLS')"),
    ):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})")
    op.execute("ALTER TABLE fleet_control DROP CONSTRAINT ck_fleet_scope")
    op.execute("ALTER TABLE fleet_control DROP COLUMN region_id")
    op.execute("ALTER TABLE fleet_position_anchor ALTER COLUMN scope TYPE VARCHAR(32)")
    op.execute("ALTER TABLE fleet_cycle ALTER COLUMN scope TYPE VARCHAR(32)")
    op.execute("ALTER TABLE fleet_control ALTER COLUMN scope TYPE VARCHAR(32)")
    op.execute(
        "ALTER TABLE fleet_control ADD CONSTRAINT ck_fleet_scope CHECK(scope='COMMITTED_FLEET')"
    )
