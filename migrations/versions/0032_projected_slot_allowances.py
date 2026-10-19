"""Projected slot inventory; retire the customer routing preparation entry point."""

from alembic import op

revision = "0032_projected_slot_allowances"
down_revision = "0031_regional_capacity_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = [
        """CREATE TABLE regional_slot_policy (
          region_id uuid NOT NULL REFERENCES operating_region,version bigint NOT NULL,
          configuration jsonb NOT NULL,authorized_by uuid NOT NULL REFERENCES app_user,
          configured_at timestamptz NOT NULL,PRIMARY KEY(region_id,version),
          CONSTRAINT ck_slot_policy_version CHECK(version>0))""",
        "CREATE TRIGGER slot_policy_immutable BEFORE UPDATE OR DELETE ON regional_slot_policy "
        "FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()",
        "CREATE TRIGGER admission_invalidate AFTER INSERT ON regional_slot_policy "
        "FOR EACH ROW EXECUTE FUNCTION admission_source_changed()",
        "ALTER TABLE regional_capacity_reservation ALTER COLUMN certificate DROP NOT NULL",
        "ALTER TABLE regional_capacity_reservation ADD COLUMN admission_basis varchar(24) "
        "NOT NULL DEFAULT 'EXACT_CERTIFICATE'",
        "ALTER TABLE regional_capacity_reservation ADD COLUMN estimated_duty_seconds "
        "bigint, ADD COLUMN estimator_policy_version bigint",
        "ALTER TABLE regional_capacity_reservation ADD CONSTRAINT ck_reservation_basis "
        "CHECK ((admission_basis='EXACT_CERTIFICATE' AND certificate IS NOT NULL) OR "
        "(admission_basis='PROJECTED_ALLOWANCE' AND certificate IS NULL AND "
        "estimated_duty_seconds IS NOT NULL AND estimator_policy_version IS NOT NULL AND "
        "estimated_duty_seconds > 0 AND estimator_policy_version > 0))",
        "ALTER TABLE regional_capacity_reservation ADD FOREIGN KEY "
        "(region_id,estimator_policy_version) "
        "REFERENCES regional_slot_policy(region_id,version)",
        "ALTER TABLE regional_slot_projection ADD COLUMN generation bigint NOT NULL DEFAULT "
        "0, ADD COLUMN policy_version bigint NOT NULL DEFAULT 0, ADD COLUMN "
        "remaining_bookings bigint NOT NULL DEFAULT 0",
        """CREATE TABLE regional_capacity_budget (
          region_id uuid NOT NULL REFERENCES operating_region,
          bucket_start timestamptz NOT NULL, kind varchar(10) NOT NULL,
          generation bigint NOT NULL, capacity_seconds bigint NOT NULL, used_seconds bigint NOT
          NULL,
          PRIMARY KEY(region_id,bucket_start,kind),
          CONSTRAINT ck_budget_kind CHECK(kind IN ('DUTY','PICKUP')),
          CONSTRAINT ck_budget_values CHECK(generation>0 AND capacity_seconds>=0 AND
          used_seconds>=0))""",
        """CREATE TABLE slot_reservation_debit (
          reservation_id uuid NOT NULL REFERENCES regional_capacity_reservation,
          bucket_start timestamptz NOT NULL, kind varchar(10) NOT NULL,
          region_id uuid NOT NULL, generation bigint NOT NULL, seconds bigint NOT NULL,
          PRIMARY KEY(reservation_id,bucket_start,kind),
          FOREIGN KEY(region_id,bucket_start,kind) REFERENCES regional_capacity_budget,
          CONSTRAINT ck_slot_debit_values CHECK(seconds>0 AND generation>0))""",
        """CREATE FUNCTION debit_slot_capacity() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
          IF NOT EXISTS(SELECT 1 FROM regional_capacity_reservation r
            WHERE r.reservation_id=NEW.reservation_id AND r.region_id=NEW.region_id
              AND r.admission_basis='PROJECTED_ALLOWANCE') THEN
            RAISE EXCEPTION 'projected reservation required' USING ERRCODE='23514'; END IF;
          UPDATE regional_capacity_budget SET used_seconds=used_seconds+NEW.seconds
            WHERE region_id=NEW.region_id AND bucket_start=NEW.bucket_start AND kind=NEW.kind
              AND generation=NEW.generation AND used_seconds+NEW.seconds<=capacity_seconds;
          IF NOT FOUND THEN RAISE EXCEPTION 'slot allowance exhausted or generation changed'
            USING ERRCODE='23514'; END IF;
          RETURN NEW;
        END $$""",
        "CREATE TRIGGER debit_slot_capacity BEFORE INSERT ON slot_reservation_debit FOR "
        "EACH ROW EXECUTE FUNCTION debit_slot_capacity()",
        "CREATE TRIGGER slot_debit_immutable BEFORE UPDATE OR DELETE ON "
        "slot_reservation_debit FOR EACH ROW EXECUTE FUNCTION "
        "reject_route_history_mutation()",
        """CREATE FUNCTION require_slot_debits() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
          IF NEW.admission_basis='PROJECTED_ALLOWANCE' AND (
            NOT EXISTS(SELECT 1 FROM slot_reservation_debit WHERE reservation_id=NEW.reservation_id
              AND kind='PICKUP' AND bucket_start=NEW.slot_start AND seconds=NEW.pickup_seconds)
            OR coalesce((SELECT sum(seconds) FROM slot_reservation_debit
              WHERE reservation_id=NEW.reservation_id AND
          kind='DUTY'),0)<>NEW.estimated_duty_seconds)
          THEN RAISE EXCEPTION 'projection hold requires complete capacity debits'
            USING ERRCODE='23514'; END IF;
          RETURN NULL;
        END $$""",
        "CREATE CONSTRAINT TRIGGER require_slot_debits AFTER INSERT ON "
        "regional_capacity_reservation DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE "
        "FUNCTION require_slot_debits()",
        "ALTER FUNCTION routing_capacity_committed(uuid,timestamptz) RENAME TO "
        "routing_exact_capacity_committed",
        """CREATE FUNCTION routing_capacity_committed(resource uuid,at_time timestamptz)
        RETURNS boolean LANGUAGE sql STABLE AS $$
          SELECT routing_exact_capacity_committed(resource,at_time) OR EXISTS (
            SELECT 1 FROM operating_region_vehicle rv
            JOIN operating_shift s ON s.vehicle_id=rv.vehicle_id AND s.closed_at IS NULL
            WHERE rv.vehicle_id=resource AND EXISTS (
              SELECT 1 FROM regional_capacity_reservation r
              LEFT JOIN collection_request b ON b.request_id=r.request_id
              WHERE r.region_id=rv.region_id AND r.admission_basis='PROJECTED_ALLOWANCE'
                AND ((r.request_id IS NULL AND r.expires_at>at_time) OR
                  b.status IN ('ACCEPTED','PRE_PLANNING','PLANNED') OR
                  (b.status='PENDING_PAYMENT' AND b.payment_expires_at>at_time)))
            AND (NOT EXISTS(SELECT 1 FROM regional_slot_projection p
                JOIN admission_source_head h USING(region_id)
                WHERE p.region_id=rv.region_id AND p.source_version=h.version
                  AND p.valid_until>at_time AND p.policy_version>0)
              OR EXISTS(SELECT 1 FROM regional_capacity_budget c
                WHERE c.region_id=rv.region_id AND c.used_seconds>0
                  AND c.bucket_start+interval '30 minutes'>at_time AND
                  c.used_seconds>greatest(0,c.capacity_seconds-greatest(0,
                    extract(epoch FROM least(c.bucket_start+interval '30 minutes',
                      greatest(s.ends_at,coalesce((SELECT max(e.ends_at)
                        FROM operating_shift_extension e WHERE e.shift_id=s.shift_id),s.ends_at)))
                      - greatest(c.bucket_start,s.starts_at))::bigint))))
          )
        $$""",
        "UPDATE regional_slot_projection SET status='UNKNOWN'",
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM regional_capacity_reservation
      WHERE admission_basis='PROJECTED_ALLOWANCE')
      OR EXISTS(SELECT 1 FROM regional_slot_policy) THEN
      RAISE EXCEPTION 'projection reservation history requires export before downgrade'; END IF;
      END $$""")
    for statement in [
        "DROP FUNCTION routing_capacity_committed(uuid,timestamptz)",
        "ALTER FUNCTION routing_exact_capacity_committed(uuid,timestamptz) RENAME TO "
        "routing_capacity_committed",
        "DROP TRIGGER require_slot_debits ON regional_capacity_reservation",
        "DROP FUNCTION require_slot_debits()",
        "DROP TABLE slot_reservation_debit",
        "DROP FUNCTION debit_slot_capacity()",
        "DROP TABLE regional_capacity_budget",
        "ALTER TABLE regional_slot_projection DROP COLUMN generation, DROP COLUMN "
        "policy_version, DROP COLUMN remaining_bookings",
        "ALTER TABLE regional_capacity_reservation DROP CONSTRAINT ck_reservation_basis, "
        "DROP COLUMN admission_basis, DROP COLUMN estimated_duty_seconds, DROP COLUMN "
        "estimator_policy_version",
        "ALTER TABLE regional_capacity_reservation ALTER COLUMN certificate SET NOT NULL",
        "DROP TABLE regional_slot_policy",
    ]:
        op.execute(statement)
