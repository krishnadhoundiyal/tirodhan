"""Entity-specific master revisions and configured flat pickup pricing."""

from alembic import op

revision = "0033_manager_business_history"
down_revision = "0032_projected_slot_allowances"
branch_labels = None
depends_on = None

# Frozen migration inventory; do not import evolving application metadata.
SOURCES = {
    "catalogue_group": "group_id",
    "catalogue_category": "category_id",
    "catalogue_media": "media_id",
    "catalogue_artwork": "artwork_id",
    "pickup_price": "price_id",
    "rider_profile": "rider_id",
    "user_role": "user_role_id",
    "fleet": "fleet_id",
    "fleet_membership": "fleet_membership_id",
    "fleet_service_cell": "fleet_service_cell_id",
    "rider_service_cell": "rider_service_cell_id",
    "operating_vehicle": "vehicle_id",
    "operating_shift": "shift_id",
    "receiving_point": "receiving_point_id",
    "operating_region": "region_id",
    "operating_region_vehicle": "vehicle_id",
    "rider_assignment": "assignment_id",
    "fleet_control": "scope",
}

FACTS = (
    "routing_receiving_window",
    "routing_facility_restriction",
    "operating_break",
    "operating_shift_extension",
    "shift_extension_consent",
    "routing_shift_confirmation",
    "regional_slot_policy",
)


def upgrade() -> None:
    for source in FACTS:
        # Existing facts have unknown command/record time; do not fabricate attribution.
        op.execute(
            f"ALTER TABLE {source} ADD COLUMN recorded_command_id uuid, "
            "ADD COLUMN recorded_at timestamptz"
        )
        op.execute(
            f"ALTER TABLE {source} ALTER COLUMN recorded_command_id SET DEFAULT "
            "nullif(current_setting('tirodhan.command',true),'')::uuid, "
            "ALTER COLUMN recorded_at SET DEFAULT CURRENT_TIMESTAMP"
        )
    op.execute("""CREATE TABLE pickup_price (
      price_id uuid PRIMARY KEY, amount_minor bigint NOT NULL, currency varchar(3) NOT NULL,
      active boolean NOT NULL,
      CONSTRAINT ck_pickup_flat_price CHECK(amount_minor>=100 AND currency='INR'))""")
    op.execute("CREATE UNIQUE INDEX uq_pickup_active_price ON pickup_price(active) WHERE active")
    for source, key in SOURCES.items():
        identity_type = "varchar(64)" if source == "fleet_control" else "uuid"
        op.execute(f"""CREATE TABLE {source}_revision (
          entity_id {identity_type} NOT NULL REFERENCES {source}({key}), revision bigint NOT NULL,
          previous_state jsonb, resulting_state jsonb NOT NULL, changed_fields varchar[] NOT NULL,
          actor_id uuid REFERENCES app_user(user_id), command_id uuid, reason varchar(100),
          changed_at timestamptz NOT NULL, PRIMARY KEY(entity_id,revision),
          CONSTRAINT ck_{source}_revision_positive CHECK(revision>0))""")
        op.execute(
            f"CREATE TRIGGER history_immutable BEFORE UPDATE OR DELETE ON {source}_revision "
            "FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()"
        )
    op.execute("""CREATE FUNCTION capture_business_revision() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE identity text; prior jsonb; resulting jsonb; n bigint; changed varchar[];
            key_type text;
    BEGIN
      identity := to_jsonb(NEW)->>TG_ARGV[0];
      key_type := CASE WHEN TG_TABLE_NAME='fleet_control' THEN 'varchar' ELSE 'uuid' END;
      resulting := to_jsonb(NEW);
      IF TG_TABLE_NAME='fleet_control' THEN
        resulting := jsonb_build_object('scope',NEW.scope,'region_id',NEW.region_id,
          'manager_id',NEW.manager_id,'version',NEW.version,'configuration',NEW.configuration);
      END IF;
      IF TG_TABLE_NAME='receiving_point' THEN
        resulting := (resulting-'location') || jsonb_build_object(
          'longitude',ST_X(NEW.location::geometry),'latitude',ST_Y(NEW.location::geometry));
      END IF;
      IF TG_TABLE_NAME='operating_region' THEN
        SELECT resulting || jsonb_build_object(
          'cells',coalesce((SELECT jsonb_agg(cell_id ORDER BY cell_id) FROM operating_region_cell
                           WHERE region_id=identity::uuid),'[]'::jsonb),
          'receiving_points',coalesce((SELECT jsonb_agg(
             receiving_point_id ORDER BY receiving_point_id)
             FROM operating_region_receiving_point WHERE region_id=identity::uuid),'[]'::jsonb),
          'vehicles',coalesce((SELECT jsonb_agg(vehicle_id ORDER BY vehicle_id)
             FROM operating_region_vehicle WHERE region_id=identity::uuid),'[]'::jsonb))
          INTO resulting;
      END IF;
      EXECUTE format('SELECT resulting_state,revision FROM %I WHERE entity_id=$1::%s
                     ORDER BY revision DESC LIMIT 1',
                     TG_TABLE_NAME || '_revision', key_type) INTO prior,n USING identity;
      IF prior IS NOT DISTINCT FROM resulting THEN RETURN NEW; END IF;
      SELECT coalesce(array_agg(key ORDER BY key),'{}'::varchar[]) INTO changed
      FROM jsonb_each(resulting) WHERE prior->key IS DISTINCT FROM value;
      EXECUTE format('INSERT INTO %I(entity_id,revision,previous_state,resulting_state,
          changed_fields,
          actor_id,command_id,reason,changed_at)
          VALUES($1::%s,$2,$3,$4,$5,$6,$7,$8,clock_timestamp())',
          TG_TABLE_NAME || '_revision', key_type)
          USING identity,coalesce(n,0)+1,prior,resulting,changed,
          nullif(current_setting('tirodhan.actor',true),'')::uuid,
          nullif(current_setting('tirodhan.command',true),'')::uuid,
          nullif(current_setting('tirodhan.reason',true),'');
      RETURN NEW;
    END $$""")
    for source, key in SOURCES.items():
        snapshot = "to_jsonb(s)"
        if source == "fleet_control":
            snapshot = """jsonb_build_object('scope',s.scope,'region_id',s.region_id,
              'manager_id',s.manager_id,'version',s.version,'configuration',s.configuration)"""
        if source == "receiving_point":
            snapshot = """(to_jsonb(s)-'location') || jsonb_build_object(
                'longitude',ST_X(s.location::geometry),'latitude',ST_Y(s.location::geometry))"""
        if source == "operating_region":
            snapshot = """to_jsonb(s) || jsonb_build_object(
                'cells',coalesce((SELECT jsonb_agg(cell_id ORDER BY cell_id)
                  FROM operating_region_cell WHERE region_id=s.region_id),'[]'::jsonb),
                'vehicles',coalesce((SELECT jsonb_agg(vehicle_id ORDER BY vehicle_id)
                  FROM operating_region_vehicle WHERE region_id=s.region_id),'[]'::jsonb),
                'receiving_points',coalesce((SELECT jsonb_agg(receiving_point_id
                  ORDER BY receiving_point_id) FROM operating_region_receiving_point
                  WHERE region_id=s.region_id),'[]'::jsonb))"""
        op.execute(f"""INSERT INTO {source}_revision(entity_id,revision,resulting_state,
          changed_fields,reason,changed_at) SELECT {key},1,{snapshot},ARRAY['BASELINE']::varchar[],
          'PRE_MIGRATION_BASELINE',clock_timestamp() FROM {source} s""")
        if source == "operating_region":
            op.execute(
                f"CREATE CONSTRAINT TRIGGER business_revision AFTER INSERT OR UPDATE ON {source} "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                f"EXECUTE FUNCTION capture_business_revision('{key}')"
            )
        else:
            op.execute(
                f"CREATE TRIGGER business_revision AFTER INSERT OR UPDATE ON {source} "
                f"FOR EACH ROW EXECUTE FUNCTION capture_business_revision('{key}')"
            )
    # Media object identities cannot be overwritten in place. New artwork needs a new asset.
    op.execute("""CREATE FUNCTION protect_product_asset_identity() RETURNS trigger
      LANGUAGE plpgsql AS $$
      BEGIN IF NEW.object_key IS DISTINCT FROM OLD.object_key THEN
        RAISE EXCEPTION 'product asset identity is immutable'; END IF; RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER product_asset_identity BEFORE UPDATE ON catalogue_media "
        "FOR EACH ROW EXECUTE FUNCTION protect_product_asset_identity()"
    )
    op.execute("""CREATE FUNCTION protect_catalogue_code() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF to_jsonb(NEW)->TG_ARGV[0] IS DISTINCT FROM to_jsonb(OLD)->TG_ARGV[0] THEN
        RAISE EXCEPTION 'catalogue code is immutable'; END IF; RETURN NEW; END $$""")
    for source, key in (("catalogue_group", "group_code"), ("catalogue_category", "category_code")):
        op.execute(
            f"CREATE TRIGGER catalogue_code BEFORE UPDATE ON {source} "
            f"FOR EACH ROW EXECUTE FUNCTION protect_catalogue_code('{key}')"
        )


def downgrade() -> None:
    # Baseline-only rows are compatible with downgrade; new attributed history is not.
    for source in SOURCES:
        op.execute(f"""DO $$ BEGIN IF EXISTS(SELECT 1 FROM {source}_revision
          WHERE revision<>1 OR actor_id IS NOT NULL OR command_id IS NOT NULL
             OR reason IS DISTINCT FROM 'PRE_MIGRATION_BASELINE') THEN
          RAISE EXCEPTION 'export adopted business revision history before downgrade';
          END IF; END $$""")
    for source in FACTS:
        op.execute(f"""DO $$ BEGIN IF EXISTS(SELECT 1 FROM {source}
          WHERE recorded_at IS NOT NULL) THEN
          RAISE EXCEPTION 'export adopted business fact attribution before downgrade';
          END IF; END $$""")
        op.execute(f"ALTER TABLE {source} DROP COLUMN recorded_command_id, DROP COLUMN recorded_at")
    for source in ("catalogue_group", "catalogue_category"):
        op.execute(f"DROP TRIGGER catalogue_code ON {source}")
    op.execute("DROP FUNCTION protect_catalogue_code()")
    op.execute("DROP TRIGGER product_asset_identity ON catalogue_media")
    op.execute("DROP FUNCTION protect_product_asset_identity()")
    for source in SOURCES:
        op.execute(f"DROP TRIGGER business_revision ON {source}")
        op.execute(f"DROP TABLE {source}_revision")
    op.execute("DROP FUNCTION capture_business_revision()")
    op.execute("DROP TABLE pickup_price")
