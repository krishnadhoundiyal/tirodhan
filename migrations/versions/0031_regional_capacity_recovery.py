"""Single-owner regional transfers, replacement workflow and advisory slot refresh."""

import re

from alembic import op

revision = "0031_regional_capacity_recovery"
down_revision = "0030_admission_certificates"
branch_labels = None
depends_on = None


def sql(script: str) -> None:
    statement, quoted = "", False
    for part in re.split(r"(\$\$)", script):
        if part == "$$":
            quoted = not quoted
            statement += part
        elif quoted:
            statement += part
        else:
            pieces = part.split(";")
            for piece in pieces[:-1]:
                statement += piece
                if statement.strip():
                    op.execute(statement)
                statement = ""
            statement += pieces[-1]
    if statement.strip():
        op.execute(statement)


def upgrade() -> None:
    op.execute("DROP TRIGGER orchestration_history_immutable ON operating_region_vehicle")
    op.execute("ALTER TABLE operating_region_vehicle ADD COLUMN version bigint NOT NULL DEFAULT 1")
    sql("""CREATE TABLE region_capacity_transfer (
      transfer_id uuid PRIMARY KEY, vehicle_id uuid NOT NULL REFERENCES operating_vehicle,
      rider_id uuid NOT NULL REFERENCES rider_profile,
      from_region_id uuid NOT NULL REFERENCES operating_region,
      to_region_id uuid NOT NULL REFERENCES operating_region,
      membership_version bigint NOT NULL, authorized_by uuid NOT NULL REFERENCES app_user,
      transferred_at timestamptz NOT NULL,
      CONSTRAINT ck_capacity_transfer_regions CHECK(from_region_id <> to_region_id));
      CREATE UNIQUE INDEX uq_capacity_transfer_version
        ON region_capacity_transfer(vehicle_id,membership_version);
      CREATE TRIGGER capacity_transfer_immutable BEFORE UPDATE OR DELETE ON region_capacity_transfer
        FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation();
      CREATE TABLE rider_replacement (
        replacement_id uuid PRIMARY KEY, request_id uuid NOT NULL REFERENCES collection_request,
        original_rider_id uuid NOT NULL REFERENCES rider_profile,
        region_id uuid NOT NULL REFERENCES operating_region,
        status varchar(24) NOT NULL, version bigint NOT NULL DEFAULT 1,
        requested_at timestamptz NOT NULL,
        recommended_shift_id uuid REFERENCES operating_shift,
        source_version bigint, recommended_at timestamptz,
        decided_by uuid REFERENCES app_user, decided_at timestamptz,
        transfer_intent_id uuid REFERENCES cargo_transfer_intent, failure_code varchar(100),
        next_attempt_at timestamptz NOT NULL, attempt bigint NOT NULL DEFAULT 0,
        CONSTRAINT ck_replacement_status CHECK(status IN
          ('REQUESTED','RECOMMENDED','APPROVED','REJECTED','COMPLETED')));
      CREATE UNIQUE INDEX uq_open_rider_replacement ON rider_replacement(request_id)
        WHERE status NOT IN ('REJECTED','COMPLETED');
      CREATE INDEX ix_replacement_region_due
        ON rider_replacement(region_id,next_attempt_at,requested_at);
      CREATE TABLE rider_replacement_decision (
        decision_id uuid PRIMARY KEY, replacement_id uuid NOT NULL REFERENCES rider_replacement,
        manager_id uuid NOT NULL REFERENCES app_user, decision varchar(16) NOT NULL,
        version bigint NOT NULL, decided_at timestamptz NOT NULL);
      CREATE TRIGGER replacement_decision_immutable BEFORE UPDATE OR DELETE
        ON rider_replacement_decision FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation();
      CREATE TABLE regional_slot_projection (
        region_id uuid NOT NULL REFERENCES operating_region, slot_start timestamptz NOT NULL,
        slot_end timestamptz NOT NULL, source_version bigint NOT NULL,
        computed_at timestamptz NOT NULL, valid_until timestamptz NOT NULL,
        status varchar(16) NOT NULL, bookings bigint NOT NULL, holds bigint NOT NULL,
        vehicle_seconds bigint NOT NULL, pickup_seconds bigint NOT NULL,
        busy_seconds bigint NOT NULL,
        PRIMARY KEY(region_id,slot_start),
        CONSTRAINT ck_slot_status CHECK(status IN ('AVAILABLE','LIMITED','FULL','UNKNOWN')),
        CONSTRAINT ck_slot_time CHECK(slot_end > slot_start AND valid_until > computed_at));
      CREATE TABLE regional_slot_refresh (
        region_id uuid PRIMARY KEY REFERENCES operating_region, source_version bigint NOT NULL,
        due_at timestamptz NOT NULL);
      CREATE INDEX ix_regional_slot_refresh_due_at ON regional_slot_refresh(due_at);
      CREATE FUNCTION invalidate_regional_slots() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        INSERT INTO regional_slot_refresh VALUES (NEW.region_id,NEW.version,clock_timestamp())
        ON CONFLICT(region_id) DO UPDATE SET source_version=excluded.source_version,
          due_at=least(regional_slot_refresh.due_at,excluded.due_at);
        RETURN NULL;
      END $$;
      CREATE TRIGGER invalidate_regional_slots AFTER INSERT OR UPDATE ON admission_source_head
        FOR EACH ROW EXECUTE FUNCTION invalidate_regional_slots();
      INSERT INTO regional_slot_refresh SELECT region_id,version,clock_timestamp()
        FROM admission_source_head;
      CREATE TRIGGER admission_invalidate AFTER UPDATE ON rider_replacement
        FOR EACH ROW WHEN (OLD.status='APPROVED' OR NEW.status='APPROVED')
        EXECUTE FUNCTION admission_source_changed();
      CREATE FUNCTION guard_region_capacity_owner() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP='DELETE' THEN
          RAISE EXCEPTION 'regional ownership must transfer explicitly'; END IF;
        IF NEW.vehicle_id<>OLD.vehicle_id OR NEW.region_id=OLD.region_id
           OR NEW.version<>OLD.version+1 OR NOT EXISTS (
          SELECT 1 FROM region_capacity_transfer t WHERE t.vehicle_id=OLD.vehicle_id
            AND t.from_region_id=OLD.region_id AND t.to_region_id=NEW.region_id
            AND t.membership_version=NEW.version) THEN
          RAISE EXCEPTION 'versioned regional transfer required';
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER regional_capacity_owner BEFORE UPDATE OR DELETE ON operating_region_vehicle
        FOR EACH ROW EXECUTE FUNCTION guard_region_capacity_owner();
      CREATE FUNCTION routing_capacity_committed(resource uuid,at_time timestamptz)
      RETURNS boolean LANGUAGE sql STABLE AS $$
        SELECT EXISTS(SELECT 1 FROM admission_allocation a
          JOIN admission_obligation o USING(obligation_id)
          LEFT JOIN regional_capacity_reservation h ON h.reservation_id=o.reservation_id
          LEFT JOIN collection_request b ON b.request_id=coalesce(o.request_id,h.request_id)
          LEFT JOIN pickup_execution p ON p.request_id=b.request_id
          WHERE a.vehicle_id=resource AND (
            (b.status IN ('ACCEPTED','PRE_PLANNING','PLANNED')
              AND (p.status IS NULL OR p.status<>'COLLECTED')
              AND NOT EXISTS(SELECT 1 FROM rider_assignment_item ai
                JOIN vehicle_route vr ON vr.assignment_id=ai.assignment_id
                WHERE ai.pickup_execution_id=p.pickup_execution_id AND ai.released_at IS NULL
                  AND vr.status<>'CLOSED' AND vr.vehicle_id<>resource))
            OR (h.expires_at>at_time AND (h.request_id IS NULL OR
              (b.status='PENDING_PAYMENT' AND b.payment_expires_at>at_time)))))
      $$;
      CREATE FUNCTION guard_shift_capacity_close() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF OLD.closed_at IS NULL AND NEW.closed_at IS NOT NULL THEN
          PERFORM 1 FROM admission_source_head WHERE region_id IN
            (SELECT region_id FROM operating_region_vehicle WHERE vehicle_id=OLD.vehicle_id)
            ORDER BY region_id FOR UPDATE;
          IF routing_capacity_committed(OLD.vehicle_id,clock_timestamp()) THEN
            RAISE EXCEPTION 'shift has certified commercial commitments' USING ERRCODE='23514';
          END IF;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER shift_capacity_close BEFORE UPDATE ON operating_shift
        FOR EACH ROW EXECUTE FUNCTION guard_shift_capacity_close();
    """)


def downgrade() -> None:
    sql("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM region_capacity_transfer) OR EXISTS(SELECT 1 FROM rider_replacement)
      THEN RAISE EXCEPTION 'capacity/replacement audit history requires export before downgrade';
      END IF;
      END $$;
      DROP TRIGGER regional_capacity_owner ON operating_region_vehicle;
      DROP TRIGGER shift_capacity_close ON operating_shift;
      DROP FUNCTION guard_shift_capacity_close(), routing_capacity_committed(uuid,timestamptz);
      CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE
        ON operating_region_vehicle FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation();
      DROP FUNCTION guard_region_capacity_owner();
      DROP TRIGGER invalidate_regional_slots ON admission_source_head;
      DROP FUNCTION invalidate_regional_slots();
      DROP TABLE regional_slot_refresh, regional_slot_projection, rider_replacement_decision,
        rider_replacement,
        region_capacity_transfer;
      ALTER TABLE operating_region_vehicle DROP COLUMN version;
    """)
