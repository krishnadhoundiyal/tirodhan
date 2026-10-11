"""Bounded Checkout readiness and concurrent observation fencing; no financial backfill."""

import sqlalchemy as sa
from alembic import op

revision = "0023_payment_checkout_readiness"
down_revision = "0022_financial_review_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payment_attempt",
        sa.Column("order_creation_pending", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("payment_attempt", sa.Column("checkout_ready_until", sa.DateTime(timezone=True)))
    op.add_column(
        "payment_attempt",
        sa.Column("observation_version", sa.Integer(), nullable=False, server_default="0"),
    )
    # Financial terms cannot drift between a verified provider snapshot and
    # Checkout, or between Order creation and a late capture. No backfill.
    op.execute("""
    CREATE FUNCTION enforce_initiated_booking_terms() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE target uuid; other_target uuid; p uuid;
    BEGIN
      IF TG_TABLE_NAME='payment' THEN
        IF ROW(NEW.amount_minor,NEW.currency) IS NOT DISTINCT FROM
           ROW(OLD.amount_minor,OLD.currency) THEN RETURN NEW; END IF;
        IF EXISTS(SELECT 1 FROM payment_attempt WHERE payment_id=OLD.payment_id) THEN
          RAISE EXCEPTION 'Initiated payment terms are immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      ELSIF TG_TABLE_NAME='collection_request' THEN
        IF ROW(NEW.quoted_amount_minor,NEW.currency) IS NOT DISTINCT FROM
           ROW(OLD.quoted_amount_minor,OLD.currency) THEN RETURN NEW; END IF;
        target := OLD.request_id;
      ELSE
        IF TG_OP='DELETE' THEN target := OLD.request_id;
        ELSE target := NEW.request_id; END IF;
        IF TG_OP='UPDATE' THEN other_target := OLD.request_id; END IF;
      END IF;
      FOR p IN SELECT payment_id FROM payment WHERE request_id IN (target,other_target)
        ORDER BY payment_id FOR UPDATE LOOP
        IF EXISTS(SELECT 1 FROM payment_attempt WHERE payment_id=p) THEN
          RAISE EXCEPTION 'Initiated booking terms are immutable' USING ERRCODE='23514';
        END IF;
      END LOOP;
      IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END $$;
    """)
    op.execute("""
    CREATE TRIGGER initiated_payment_terms BEFORE UPDATE OF amount_minor,currency ON payment
      FOR EACH ROW EXECUTE FUNCTION enforce_initiated_booking_terms();
    """)
    op.execute("""
    CREATE TRIGGER initiated_quote_terms BEFORE UPDATE OF quoted_amount_minor,currency
      ON collection_request FOR EACH ROW EXECUTE FUNCTION enforce_initiated_booking_terms();
    """)
    op.execute("""
    CREATE TRIGGER initiated_item_terms BEFORE INSERT OR UPDATE OR DELETE ON collection_request_item
      FOR EACH ROW EXECUTE FUNCTION enforce_initiated_booking_terms();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS initiated_item_terms ON collection_request_item")
    op.execute("DROP TRIGGER IF EXISTS initiated_quote_terms ON collection_request")
    op.execute("DROP TRIGGER IF EXISTS initiated_payment_terms ON payment")
    op.execute("DROP FUNCTION IF EXISTS enforce_initiated_booking_terms()")
    op.drop_column("payment_attempt", "order_creation_pending")
    op.drop_column("payment_attempt", "observation_version")
    op.drop_column("payment_attempt", "checkout_ready_until")
