"""Explicit occupied volume on existing booking items; no policy or backfill."""

from alembic import op

revision = "0038_item_volume"
down_revision = "0037_weight_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE collection_request_item ADD COLUMN declared_volume_ml bigint,
      ADD CONSTRAINT ck_collection_item_volume CHECK (
        declared_volume_ml > 0 AND declared_volume_ml <= 2147483647)""")
    # Existing booked_weight_terms already freezes every item column, including volume.
    op.execute("""CREATE FUNCTION validate_booked_volume() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE declared_count bigint; item_count bigint; volume numeric;
      BEGIN
        SELECT count(declared_volume_ml),count(*),sum(declared_volume_ml)
          INTO declared_count,item_count,volume FROM collection_request_item
          WHERE request_id=NEW.request_id;
        IF declared_count > 0 AND (declared_count <> item_count OR volume > 2147483647) THEN
          RAISE EXCEPTION 'booking requires complete bounded item volume declarations';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE CONSTRAINT TRIGGER validate_volume_quote
      AFTER INSERT ON booking_weight_quote
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_booked_volume()""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM collection_request_item WHERE declared_volume_ml IS NOT NULL) THEN
        RAISE EXCEPTION 'export declared item volumes before downgrade';
      END IF;
    END $$""")
    op.execute("DROP TRIGGER validate_volume_quote ON booking_weight_quote")
    op.execute("DROP FUNCTION validate_booked_volume()")
    op.execute("ALTER TABLE collection_request_item DROP COLUMN declared_volume_ml")
