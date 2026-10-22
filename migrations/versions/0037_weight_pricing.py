"""Versioned weight pricing and immutable declared-weight provenance; no volume policy."""

from alembic import op

revision = "0037_weight_pricing"
down_revision = "0036_recovery_work_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE weight_pricing_policy (
      policy_id uuid PRIMARY KEY,
      first_band_upper_grams bigint NOT NULL, numerical_threshold_grams bigint NOT NULL,
      first_band_label varchar(100) NOT NULL, middle_band_label varchar(100) NOT NULL,
      open_band_label varchar(100) NOT NULL, display_unit varchar(2) NOT NULL,
      minimum_amount_minor bigint NOT NULL, maximum_amount_minor bigint NOT NULL,
      included_weight_grams bigint NOT NULL, saturation_weight_grams bigint NOT NULL,
      curvature numeric(18,6) NOT NULL, currency varchar(3) NOT NULL, active boolean NOT NULL,
      adopted boolean NOT NULL DEFAULT false,
      CONSTRAINT ck_weight_policy_bands CHECK(first_band_upper_grams > 0
        AND numerical_threshold_grams > first_band_upper_grams
        AND numerical_threshold_grams <= 2147483647),
      CONSTRAINT ck_weight_policy_price CHECK(minimum_amount_minor >= 100
        AND maximum_amount_minor >= minimum_amount_minor
        AND maximum_amount_minor <= 9007199254740991 AND currency='INR'
        AND display_unit='kg' AND curvature > 0 AND included_weight_grams >= 0
        AND saturation_weight_grams > included_weight_grams
        AND saturation_weight_grams <= 2147483647))""")
    op.execute(
        "CREATE UNIQUE INDEX uq_weight_policy_active ON weight_pricing_policy(active) WHERE active"
    )
    op.execute("""CREATE TABLE weight_pricing_policy_revision (
      entity_id uuid NOT NULL REFERENCES weight_pricing_policy(policy_id), revision bigint NOT NULL,
      previous_state jsonb, resulting_state jsonb NOT NULL, changed_fields varchar[] NOT NULL,
      actor_id uuid REFERENCES app_user(user_id), command_id uuid, reason varchar(100),
      changed_at timestamptz NOT NULL, PRIMARY KEY(entity_id,revision),
      CONSTRAINT ck_weight_pricing_policy_revision_positive CHECK(revision > 0))""")
    op.execute(
        "CREATE TRIGGER history_immutable BEFORE UPDATE OR DELETE ON "
        "weight_pricing_policy_revision FOR EACH ROW "
        "EXECUTE FUNCTION reject_route_history_mutation()"
    )
    # Preserve decimal curvature as a string in history reads, avoiding a JSON client's
    # binary-float decoder changing the exact configuration shown to a manager.
    op.execute("""CREATE FUNCTION capture_weight_pricing_revision() RETURNS trigger
      LANGUAGE plpgsql AS $$
      DECLARE prior jsonb; resulting jsonb; n bigint; changed varchar[];
      BEGIN
        resulting := jsonb_set(to_jsonb(NEW),'{curvature}',to_jsonb(NEW.curvature::text));
        SELECT resulting_state,revision INTO prior,n FROM weight_pricing_policy_revision
          WHERE entity_id=NEW.policy_id ORDER BY revision DESC LIMIT 1;
        IF prior IS NOT DISTINCT FROM resulting THEN RETURN NEW; END IF;
        SELECT coalesce(array_agg(key ORDER BY key),'{}'::varchar[]) INTO changed
          FROM jsonb_each(resulting) WHERE prior->key IS DISTINCT FROM value;
        INSERT INTO weight_pricing_policy_revision(entity_id,revision,previous_state,
          resulting_state,changed_fields,actor_id,command_id,reason,changed_at)
          VALUES(NEW.policy_id,coalesce(n,0)+1,prior,resulting,changed,
            nullif(current_setting('tirodhan.actor',true),'')::uuid,
            nullif(current_setting('tirodhan.command',true),'')::uuid,
            nullif(current_setting('tirodhan.reason',true),''),clock_timestamp());
        RETURN NEW;
      END $$""")
    op.execute(
        "CREATE TRIGGER business_revision AFTER INSERT OR UPDATE ON weight_pricing_policy "
        "FOR EACH ROW EXECUTE FUNCTION capture_weight_pricing_revision()"
    )
    # Prevent removal/rebinding from accidentally restoring the legacy flat-price fallback.
    op.execute("""CREATE FUNCTION protect_weight_policy_identity() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('tirodhan.weight-pricing',0));
        IF TG_OP='DELETE' THEN
          RAISE EXCEPTION 'weight pricing adoption and identity must be retained';
        END IF;
        IF TG_OP='UPDATE' THEN
          IF NEW.policy_id IS DISTINCT FROM OLD.policy_id THEN
            RAISE EXCEPTION 'weight pricing identity is immutable';
          END IF;
          NEW.adopted := OLD.adopted OR NEW.active;
        ELSE NEW.adopted := NEW.active; END IF;
        RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER weight_policy_identity BEFORE INSERT OR UPDATE OR DELETE ON "
        "weight_pricing_policy FOR EACH ROW EXECUTE FUNCTION protect_weight_policy_identity()"
    )
    op.execute(
        "ALTER TABLE collection_request_item ADD COLUMN weight_band varchar(16), "
        "ADD COLUMN weight_kg varchar(20)"
    )
    op.execute(
        "ALTER TABLE regional_capacity_reservation ADD COLUMN declared_mass_g bigint, "
        "ADD COLUMN declaration_fingerprint varchar(64), "
        "ADD COLUMN weight_quote_fingerprint varchar(64)"
    )
    op.execute("""CREATE FUNCTION protect_hold_weight_terms() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF ROW(NEW.declared_mass_g,NEW.declaration_fingerprint,NEW.weight_quote_fingerprint)
        IS DISTINCT FROM ROW(OLD.declared_mass_g,OLD.declaration_fingerprint,
                             OLD.weight_quote_fingerprint)
        THEN RAISE EXCEPTION 'hold weight terms are immutable'; END IF; RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER hold_weight_terms BEFORE UPDATE ON regional_capacity_reservation "
        "FOR EACH ROW EXECUTE FUNCTION protect_hold_weight_terms()"
    )
    op.execute("""CREATE TABLE booking_weight_quote (
      request_id uuid PRIMARY KEY REFERENCES collection_request(request_id),
      policy_id uuid NOT NULL, policy_revision bigint NOT NULL, total_weight_grams bigint NOT NULL,
      minimum_amount_minor bigint NOT NULL, maximum_amount_minor bigint NOT NULL,
      quote_fingerprint varchar(64) NOT NULL,
      FOREIGN KEY(policy_id,policy_revision)
        REFERENCES weight_pricing_policy_revision(entity_id,revision),
      CONSTRAINT ck_booking_weight_quote CHECK(total_weight_grams > 0
        AND total_weight_grams <= 2147483647 AND minimum_amount_minor >= 100
        AND maximum_amount_minor >= minimum_amount_minor
        AND maximum_amount_minor <= 9007199254740991 AND policy_revision > 0))""")
    op.execute(
        "CREATE TRIGGER weight_quote_immutable BEFORE UPDATE OR DELETE ON "
        "booking_weight_quote FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()"
    )
    op.execute("""CREATE FUNCTION guard_booked_weight_terms() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE target uuid;
      BEGIN
        IF TG_OP='INSERT' THEN target := NEW.request_id; ELSE target := OLD.request_id; END IF;
        IF EXISTS(SELECT 1 FROM booking_weight_quote WHERE request_id=target) OR
           (TG_OP='UPDATE' AND EXISTS(SELECT 1 FROM booking_weight_quote
                                     WHERE request_id=NEW.request_id)) THEN
          RAISE EXCEPTION 'confirmed weight booking terms are immutable';
        END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
      END $$""")
    op.execute(
        "CREATE TRIGGER booked_weight_terms BEFORE INSERT OR UPDATE OR DELETE ON "
        "collection_request_item FOR EACH ROW EXECUTE FUNCTION guard_booked_weight_terms()"
    )
    op.execute("""CREATE FUNCTION validate_booked_weight_quote() RETURNS trigger
      LANGUAGE plpgsql AS $$
      DECLARE total bigint; amount bigint; line_amount bigint; valid boolean;
      BEGIN
        SELECT sum(declared_weight_grams),sum(quoted_line_amount_minor),
          bool_and(coalesce(weight_band IN ('BELOW_B','B_TO_T','ABOVE_T')
            AND declared_weight_grams > 0 AND currency='INR',false))
          INTO total,line_amount,valid FROM collection_request_item WHERE request_id=NEW.request_id;
        SELECT quoted_amount_minor INTO amount FROM collection_request
          WHERE request_id=NEW.request_id;
        IF total IS DISTINCT FROM NEW.total_weight_grams OR valid IS DISTINCT FROM true
           OR amount IS DISTINCT FROM line_amount
           OR amount < NEW.minimum_amount_minor OR amount > NEW.maximum_amount_minor
           OR NOT EXISTS(SELECT 1 FROM payment WHERE request_id=NEW.request_id
                         AND amount_minor=amount AND currency='INR') THEN
          RAISE EXCEPTION 'weight quotation must match booking items and payment';
        END IF; RETURN NEW;
      END $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER validate_weight_quote AFTER INSERT ON booking_weight_quote "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION validate_booked_weight_quote()"
    )
    op.execute("""CREATE FUNCTION guard_weight_financial_terms() RETURNS trigger
      LANGUAGE plpgsql AS $$
      BEGIN IF EXISTS(SELECT 1 FROM booking_weight_quote WHERE request_id=OLD.request_id) THEN
        IF TG_TABLE_NAME='payment' THEN
          IF ROW(NEW.amount_minor,NEW.currency) IS DISTINCT FROM ROW(OLD.amount_minor,OLD.currency)
            THEN RAISE EXCEPTION 'weight payment terms are immutable'; END IF;
        ELSE
          IF ROW(NEW.quoted_amount_minor,NEW.currency) IS DISTINCT FROM
             ROW(OLD.quoted_amount_minor,OLD.currency) THEN
            RAISE EXCEPTION 'weight booking quote is immutable'; END IF;
        END IF;
      END IF; RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER weight_payment_terms BEFORE UPDATE OF amount_minor,currency ON "
        "payment FOR EACH ROW EXECUTE FUNCTION guard_weight_financial_terms()"
    )
    op.execute(
        "CREATE TRIGGER weight_booking_terms BEFORE UPDATE OF quoted_amount_minor,currency "
        "ON collection_request FOR EACH ROW EXECUTE FUNCTION guard_weight_financial_terms()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM weight_pricing_policy) OR EXISTS(SELECT 1 FROM booking_weight_quote)
         OR EXISTS(SELECT 1 FROM regional_capacity_reservation WHERE declared_mass_g IS NOT NULL
                   OR declaration_fingerprint IS NOT NULL OR weight_quote_fingerprint IS NOT NULL)
         OR EXISTS(SELECT 1 FROM collection_request_item WHERE weight_band IS NOT NULL
                   OR weight_kg IS NOT NULL) THEN
        RAISE EXCEPTION 'export adopted weight pricing history before downgrade';
      END IF; END $$""")
    op.execute("DROP TRIGGER validate_weight_quote ON booking_weight_quote")
    op.execute("DROP TRIGGER weight_payment_terms ON payment")
    op.execute("DROP TRIGGER weight_booking_terms ON collection_request")
    op.execute("DROP FUNCTION guard_weight_financial_terms()")
    op.execute("DROP TRIGGER hold_weight_terms ON regional_capacity_reservation")
    op.execute("DROP FUNCTION protect_hold_weight_terms()")
    op.execute("DROP FUNCTION validate_booked_weight_quote()")
    op.execute("DROP TRIGGER booked_weight_terms ON collection_request_item")
    op.execute("DROP FUNCTION guard_booked_weight_terms()")
    op.execute("DROP TABLE booking_weight_quote")
    op.execute(
        "ALTER TABLE regional_capacity_reservation DROP COLUMN declared_mass_g, "
        "DROP COLUMN declaration_fingerprint, DROP COLUMN weight_quote_fingerprint"
    )
    op.execute("ALTER TABLE collection_request_item DROP COLUMN weight_band, DROP COLUMN weight_kg")
    op.execute("DROP TABLE weight_pricing_policy_revision")
    op.execute("DROP TABLE weight_pricing_policy")
    op.execute("DROP FUNCTION capture_weight_pricing_revision()")
    op.execute("DROP FUNCTION protect_weight_policy_identity()")
