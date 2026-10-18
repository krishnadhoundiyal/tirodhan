"""Versioned compositional admission certificates and transactional invalidation."""

from alembic import op

revision = "0030_admission_certificates"
down_revision = "0029_admission_retry_schedule"
branch_labels = None
depends_on = None

TABLES = (
    "admission_plan_revision",
    "admission_route_certificate",
    "admission_route_membership",
    "admission_obligation",
    "admission_allocation",
    "admission_proof_obligation",
    "admission_preparation_lease",
    "admission_source_notice",
    "admission_source_head",
)
SOURCES = (
    "collection_request",
    "regional_capacity_reservation",
    "routing_manifest",
    "operating_vehicle",
    "operating_shift",
    "vehicle_position",
    "rider_profile",
    "rider_availability",
    "app_user",
    "user_role",
    "receiving_point",
    "routing_receiving_window",
    "operating_region",
    "operating_region_cell",
    "operating_region_vehicle",
    "operating_region_receiving_point",
    "fleet_control",
    "pickup_execution",
    "pickup_attempt",
    "rider_assignment",
    "rider_assignment_item",
    "handover_event",
    "handover_event_item",
    "cargo_vehicle_observation",
    "vehicle_route",
    "route_revision",
    "route_revision_stop",
    "route_task",
    "route_recovery",
    "appointment_consent",
    "discharge_authorization",
    "operating_break",
    "operating_shift_extension",
    "cargo_transfer",
    "pickup_reattempt",
    "fleet_exception",
    "cargo_transfer_intent",
    "cargo_transfer_receipt",
    "flexible_commitment",
    "admission_flexible_commitment",
    "reattempt_authorization",
    "physical_load_observation",
    "route_resume_authorization",
    "routing_facility_restriction",
    "routing_shift_confirmation",
)


def upgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM capacity_admission_request
        WHERE status='RUNNING') THEN
        RAISE EXCEPTION 'drain regional admission workers before upgrading certificates';
        END IF; END $$""")
    op.execute(
        "CREATE INDEX ix_admission_regional_due ON capacity_admission_request "
        "(region_id,status,created_at,admission_id)"
    )
    op.execute("""CREATE TABLE admission_plan_revision (
        region_id UUID REFERENCES operating_region(region_id), revision BIGINT NOT NULL,
        format_version VARCHAR(64) NOT NULL, source_stamp BYTEA NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(region_id,revision),
        CONSTRAINT ck_admission_plan_revision CHECK(revision>0))""")
    op.execute("""CREATE TABLE admission_route_certificate (
        certificate_id UUID PRIMARY KEY, region_id UUID NOT NULL REFERENCES operating_region,
        vehicle_id UUID NOT NULL REFERENCES operating_vehicle,
        shift_id UUID NOT NULL REFERENCES operating_shift,
        fingerprint BYTEA NOT NULL, proof JSONB NOT NULL,
        valid_until TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT uq_admission_certificate_owner UNIQUE(region_id,vehicle_id,certificate_id))""")
    op.execute(
        "CREATE INDEX ix_admission_route_certificate_region_id "
        "ON admission_route_certificate(region_id)"
    )
    op.execute("""CREATE TABLE admission_route_membership (
        region_id UUID NOT NULL, vehicle_id UUID NOT NULL REFERENCES operating_vehicle,
        from_revision BIGINT NOT NULL, to_revision BIGINT,
        certificate_id UUID NOT NULL,
        FOREIGN KEY(region_id,vehicle_id,certificate_id)
          REFERENCES admission_route_certificate(region_id,vehicle_id,certificate_id),
        PRIMARY KEY(region_id,vehicle_id,from_revision),
        FOREIGN KEY(region_id,from_revision) REFERENCES admission_plan_revision,
        FOREIGN KEY(region_id,to_revision) REFERENCES admission_plan_revision,
        CONSTRAINT ck_admission_membership_interval
        CHECK(to_revision IS NULL OR to_revision>from_revision))""")
    op.execute(
        "CREATE UNIQUE INDEX uq_admission_current_route "
        "ON admission_route_membership(region_id,vehicle_id) WHERE to_revision IS NULL"
    )
    op.execute("""CREATE TABLE admission_obligation (
        obligation_id UUID PRIMARY KEY, region_id UUID NOT NULL REFERENCES operating_region,
        reservation_id UUID UNIQUE REFERENCES regional_capacity_reservation,
        request_id UUID UNIQUE REFERENCES collection_request,
        CONSTRAINT ck_admission_obligation_origin
        CHECK(reservation_id IS NOT NULL OR request_id IS NOT NULL))""")
    op.execute("CREATE INDEX ix_admission_obligation_region_id ON admission_obligation(region_id)")
    op.execute("""CREATE TABLE admission_allocation (
        obligation_id UUID PRIMARY KEY REFERENCES admission_obligation,
        vehicle_id UUID NOT NULL REFERENCES operating_vehicle)""")
    op.execute(
        "CREATE INDEX ix_admission_allocation_vehicle_id ON admission_allocation(vehicle_id)"
    )
    op.execute("""CREATE TABLE admission_proof_obligation (
        certificate_id UUID REFERENCES admission_route_certificate,
        obligation_id UUID REFERENCES admission_obligation,
        PRIMARY KEY(certificate_id,obligation_id))""")
    op.execute(
        "ALTER TABLE capacity_admission_request ADD COLUMN preparation_cursor "
        "JSONB NOT NULL DEFAULT '{}'"
    )
    op.execute("""CREATE TABLE admission_preparation_lease (
        region_id UUID PRIMARY KEY REFERENCES operating_region,
        token UUID NOT NULL, expires_at TIMESTAMPTZ NOT NULL)""")
    op.execute(
        "CREATE INDEX ix_admission_preparation_lease_expires_at "
        "ON admission_preparation_lease(expires_at)"
    )
    op.execute("""CREATE TABLE admission_source_notice (
        notice_id UUID PRIMARY KEY, region_id UUID NOT NULL REFERENCES operating_region,
        source_table VARCHAR(64) NOT NULL, source_key VARCHAR(200) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT uq_admission_source_invalidation UNIQUE(region_id,source_table,source_key))""")
    op.execute(
        "CREATE INDEX ix_admission_notice_region ON admission_source_notice(region_id,notice_id)"
    )
    op.execute("""CREATE TABLE admission_source_head (
        region_id UUID PRIMARY KEY REFERENCES operating_region,
        version BIGINT NOT NULL DEFAULT 0)""")
    op.execute(
        "INSERT INTO admission_source_head(region_id) SELECT region_id FROM operating_region"
    )
    op.execute("""CREATE FUNCTION admission_source_changed() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE payload JSONB; target UUID; request UUID; vehicle UUID; rider UUID;
            pickup UUID; route UUID; shift UUID; assignment UUID; point UUID;
            targets UUID[]; resource_ids UUID[]; identity TEXT;
    BEGIN
      FOR payload IN SELECT to_jsonb(NEW) WHERE TG_OP<>'DELETE'
                     UNION SELECT to_jsonb(OLD) WHERE TG_OP<>'INSERT' LOOP
        identity := coalesce(payload->>'request_id', payload->>'reservation_id',
          payload->>'vehicle_id', payload->>'rider_id', payload->>'user_id',
          payload->>'receiving_point_id', payload->>'region_id', 'scope');
        request := nullif(payload->>'request_id','')::uuid;
        vehicle := nullif(payload->>'vehicle_id','')::uuid;
        rider := coalesce(nullif(payload->>'rider_id',''),nullif(payload->>'user_id',''))::uuid;
        pickup := nullif(payload->>'pickup_execution_id','')::uuid;
        IF pickup IS NULL AND coalesce(payload->>'attempt_id',payload->>'pickup_attempt_id')
           IS NOT NULL THEN
          SELECT pickup_execution_id INTO pickup FROM pickup_attempt
          WHERE pickup_attempt_id=
            coalesce(payload->>'attempt_id',payload->>'pickup_attempt_id')::uuid;
        END IF;
        IF request IS NULL AND pickup IS NOT NULL THEN
          SELECT request_id INTO request FROM pickup_execution WHERE pickup_execution_id=pickup;
        END IF;
        route := nullif(payload->>'route_id','')::uuid;
        IF route IS NULL AND payload->>'revision_id' IS NOT NULL THEN
          SELECT route_id INTO route FROM route_revision
          WHERE revision_id=(payload->>'revision_id')::uuid;
        END IF;
        IF route IS NULL AND payload->>'task_id' IS NOT NULL THEN
          SELECT route_id INTO route FROM route_task WHERE task_id=(payload->>'task_id')::uuid;
        END IF;
        shift := nullif(payload->>'shift_id','')::uuid;
        assignment := coalesce(nullif(payload->>'assignment_id',''),
                               nullif(payload->>'rider_assignment_id',''))::uuid;
        IF rider IS NULL AND assignment IS NOT NULL THEN
          SELECT rider_id INTO rider FROM rider_assignment WHERE assignment_id=assignment;
        END IF;
        resource_ids := ARRAY[vehicle,nullif(payload->>'from_vehicle_id','')::uuid,
                            nullif(payload->>'to_vehicle_id','')::uuid];
        IF route IS NOT NULL THEN
          resource_ids := resource_ids || ARRAY(SELECT vehicle_id FROM vehicle_route
                                               WHERE route_id=route);
        END IF;
        IF shift IS NOT NULL OR payload->>'to_shift_id' IS NOT NULL THEN
          resource_ids := resource_ids || ARRAY(SELECT vehicle_id FROM operating_shift
            WHERE shift_id=ANY(ARRAY[shift,nullif(payload->>'to_shift_id','')::uuid]));
        END IF;
        IF payload->>'intent_id' IS NOT NULL THEN
          resource_ids := resource_ids || ARRAY(SELECT t.from_vehicle_id
            FROM cargo_transfer_intent t WHERE t.intent_id=(payload->>'intent_id')::uuid);
          resource_ids := resource_ids || ARRAY(SELECT s.vehicle_id FROM cargo_transfer_intent t
            JOIN operating_shift s ON s.shift_id=t.to_shift_id
            WHERE t.intent_id=(payload->>'intent_id')::uuid);
        END IF;
        identity := coalesce(request::text,vehicle::text,rider::text,
                             route::text,shift::text,identity);
        targets := ARRAY[]::uuid[];
        IF TG_TABLE_NAME='user_role' AND payload->>'role_code'<>'RIDER' THEN
          CONTINUE;
        END IF;
        IF TG_TABLE_NAME IN ('app_user','user_role') AND NOT EXISTS(
          SELECT 1 FROM operating_shift WHERE rider_id=rider) THEN
          CONTINUE;
        END IF;
        IF payload->>'region_id' IS NOT NULL THEN
          targets := array_append(targets,(payload->>'region_id')::uuid);
        END IF;
        IF payload->>'cell_id' IS NOT NULL THEN
          targets := targets || ARRAY(SELECT region_id FROM operating_region_cell
                                     WHERE cell_id=payload->>'cell_id');
        END IF;
        IF request IS NOT NULL THEN
          targets := targets || ARRAY(SELECT rc.region_id FROM collection_request cr
            JOIN operating_region_cell rc ON rc.cell_id=cr.cell_id WHERE cr.request_id=request);
          targets := targets || ARRAY(SELECT rv.region_id FROM pickup_execution pe
            JOIN rider_assignment_item ai USING(pickup_execution_id)
            JOIN rider_assignment a USING(assignment_id)
            JOIN operating_shift s ON s.rider_id=a.rider_id
            JOIN operating_region_vehicle rv USING(vehicle_id) WHERE pe.request_id=request);
          targets := targets || ARRAY(SELECT rv.region_id FROM cargo_transfer ct
            JOIN operating_region_vehicle rv ON rv.vehicle_id=ct.to_vehicle_id
            WHERE ct.request_id=request);
        END IF;
        targets := targets || ARRAY(SELECT region_id FROM operating_region_vehicle
                                   WHERE vehicle_id=ANY(resource_ids));
        IF TG_TABLE_NAME IN ('receiving_point','routing_receiving_window',
                             'routing_facility_restriction') THEN
          point := nullif(payload->>'receiving_point_id','')::uuid;
          targets := targets || ARRAY(SELECT region_id FROM operating_region_receiving_point
                                     WHERE receiving_point_id=point);
          targets := targets || ARRAY(SELECT region_id FROM operating_region
                                     WHERE default_warehouse_id=point);
          targets := targets || ARRAY(SELECT rv.region_id FROM operating_vehicle v
            JOIN operating_region_vehicle rv USING(vehicle_id)
            WHERE v.end_receiving_point_id=point);
          targets := targets || ARRAY(SELECT rc.region_id FROM routing_manifest m
            JOIN collection_request cr USING(request_id)
            JOIN operating_region_cell rc ON rc.cell_id=cr.cell_id
            WHERE m.receiving_point_id=point);
        END IF;
        IF rider IS NOT NULL THEN
          targets := targets || ARRAY(SELECT rv.region_id FROM operating_shift s
            JOIN operating_region_vehicle rv USING(vehicle_id) WHERE s.rider_id=rider);
        END IF;
        -- Unresolved/global dependencies invalidate conservatively. Known domain
        -- FK closures above keep ordinary execution and telemetry region-local.
        IF cardinality(targets)=0 THEN
          targets := ARRAY(SELECT region_id FROM operating_region);
        END IF;
        FOR target IN SELECT region_id FROM operating_region
                      WHERE region_id=ANY(targets) ORDER BY region_id LOOP
          -- Coordinate with admission's short publication lock even when the
          -- source writer uses READ COMMITTED. No lock is held during road work.
          INSERT INTO admission_source_head(region_id,version) VALUES(target,1)
          ON CONFLICT(region_id) DO UPDATE SET version=admission_source_head.version+1;
          INSERT INTO admission_source_notice VALUES
            (gen_random_uuid(),target,TG_TABLE_NAME,left(identity,200),clock_timestamp())
          ON CONFLICT(region_id,source_table,source_key) DO UPDATE
            SET notice_id=excluded.notice_id, created_at=excluded.created_at;
        END LOOP;
      END LOOP;
      RETURN NULL;
    END $$""")
    for table in SOURCES:
        op.execute(
            f"CREATE TRIGGER admission_invalidate AFTER INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION admission_source_changed()"
        )
    op.execute("""CREATE FUNCTION protect_admission_proof() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN RAISE EXCEPTION 'admission certificate history is immutable'; END $$""")
    for table in (
        "admission_plan_revision",
        "admission_route_certificate",
        "admission_proof_obligation",
    ):
        op.execute(
            f"CREATE TRIGGER immutable_admission BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION protect_admission_proof()"
        )

    op.execute("""CREATE FUNCTION protect_admission_membership() RETURNS trigger
    LANGUAGE plpgsql AS $$ BEGIN
      IF TG_OP='UPDATE' AND OLD.to_revision IS NULL AND NEW.to_revision IS NOT NULL
         AND (to_jsonb(OLD)-'to_revision')=(to_jsonb(NEW)-'to_revision') THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'admission membership can only be closed once';
    END $$""")
    op.execute(
        "CREATE TRIGGER immutable_admission_membership BEFORE UPDATE OR DELETE "
        "ON admission_route_membership FOR EACH ROW "
        "EXECUTE FUNCTION protect_admission_membership()"
    )
    op.execute("""CREATE FUNCTION protect_admission_obligation() RETURNS trigger
    LANGUAGE plpgsql AS $$ BEGIN
      IF TG_OP='UPDATE' AND (OLD.request_id IS NOT NULL OR NEW.request_id IS NULL OR
          (to_jsonb(OLD)-'request_id')<>(to_jsonb(NEW)-'request_id')) THEN
        RAISE EXCEPTION 'admission obligation binds once';
      END IF;
      IF NEW.reservation_id IS NOT NULL AND NOT EXISTS(
          SELECT 1 FROM regional_capacity_reservation r WHERE r.reservation_id=NEW.reservation_id
          AND r.region_id=NEW.region_id AND
          (NEW.request_id IS NULL OR NEW.request_id=r.request_id)) THEN
        RAISE EXCEPTION 'admission obligation reservation mismatch';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER admission_obligation_alias BEFORE INSERT OR UPDATE "
        "ON admission_obligation FOR EACH ROW "
        "EXECUTE FUNCTION protect_admission_obligation()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM admission_plan_revision) THEN
       RAISE EXCEPTION 'admission certificate history requires export/drain before downgrade';
       END IF; END $$""")
    op.execute("DROP INDEX ix_admission_regional_due")
    op.execute("ALTER TABLE capacity_admission_request DROP COLUMN preparation_cursor")
    for table in SOURCES:
        op.execute(f"DROP TRIGGER admission_invalidate ON {table}")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute(
        "DROP FUNCTION admission_source_changed(), protect_admission_proof(), "
        "protect_admission_membership(), protect_admission_obligation()"
    )
