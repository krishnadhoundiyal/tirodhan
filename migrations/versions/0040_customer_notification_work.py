"""Incremental customer event work and indexed registration admission history."""

from alembic import op

revision = "0040_customer_notification_work"
down_revision = "0039_customer_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No silent legacy-device eviction: an operator must resolve any pre-policy excess.
    op.execute("""DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM customer_push_binding b JOIN refresh_session s
          ON s.refresh_session_id=b.session_id
        WHERE b.revoked_at IS NULL AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
        GROUP BY b.user_id HAVING count(*) FILTER (WHERE b.platform='WEB')>10
          OR count(*) FILTER (WHERE b.platform<>'WEB')>5
      ) THEN RAISE EXCEPTION 'CUSTOMER_DEVICE_POLICY_REQUIRES_REVOCATION'; END IF;
    END $$""")
    op.execute("""CREATE INDEX ix_customer_binding_changes
      ON customer_push_binding(user_id,created_at)""")
    op.execute("""CREATE INDEX ix_customer_binding_active
      ON customer_push_binding(user_id,platform) WHERE revoked_at IS NULL""")
    op.execute("""CREATE TABLE customer_notification_pending (
      source_event_id UUID PRIMARY KEY REFERENCES outbox_event(outbox_event_id),
      available_at TIMESTAMPTZ NOT NULL)""")
    op.execute("""CREATE INDEX ix_customer_notification_due
      ON customer_notification_pending(available_at,source_event_id)""")
    # The insert/update trigger and historical adoption are installed in the same migration
    # transaction. PostgreSQL DDL locks fence source writers across this cutover.
    op.execute("""CREATE FUNCTION customer_notification_enqueue() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.event_type IN ('CollectionRequestAccepted','CustomerFinancialStatusChanged')
          AND NOT EXISTS (SELECT 1 FROM customer_notification n
            WHERE n.source_event_id=NEW.outbox_event_id) THEN
          INSERT INTO customer_notification_pending(source_event_id,available_at)
            VALUES (NEW.outbox_event_id,NEW.available_at)
          ON CONFLICT (source_event_id) DO UPDATE SET available_at=EXCLUDED.available_at;
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("""CREATE TRIGGER customer_notification_enqueue
      AFTER INSERT OR UPDATE OF available_at ON outbox_event
      FOR EACH ROW EXECUTE FUNCTION customer_notification_enqueue()""")
    op.execute("""INSERT INTO customer_notification_pending(source_event_id,available_at)
      SELECT e.outbox_event_id,e.available_at FROM outbox_event e
      WHERE e.event_type IN ('CollectionRequestAccepted','CustomerFinancialStatusChanged')
      AND NOT EXISTS (SELECT 1 FROM customer_notification n
        WHERE n.source_event_id=e.outbox_event_id)""")


def downgrade() -> None:
    # Derived membership can be safely reconstructed by either version from retained facts.
    op.execute("DROP TRIGGER customer_notification_enqueue ON outbox_event")
    op.execute("DROP FUNCTION customer_notification_enqueue()")
    op.execute("DROP TABLE customer_notification_pending")
    op.execute("DROP INDEX ix_customer_binding_changes")
    op.execute("DROP INDEX ix_customer_binding_active")
