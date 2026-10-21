"""Versioned replacement choices, real incident attribution and activation lineage."""

from alembic import op

revision = "0034_replacement_options"
down_revision = "0033_manager_business_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE replacement_evaluation (
      evaluation_id uuid PRIMARY KEY, replacement_id uuid NOT NULL REFERENCES rider_replacement,
      replacement_version bigint NOT NULL, region_version bigint NOT NULL,
      source_version bigint NOT NULL, source_fingerprint bytea NOT NULL,
      created_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
      outcome varchar(24) NOT NULL, search jsonb NOT NULL)""")
    op.execute(
        "CREATE INDEX ix_replacement_evaluation_history ON replacement_evaluation "
        "(replacement_id,created_at,evaluation_id)"
    )
    op.execute("""CREATE TABLE replacement_candidate (
      candidate_id uuid PRIMARY KEY, evaluation_id uuid NOT NULL REFERENCES replacement_evaluation,
      shift_id uuid NOT NULL REFERENCES operating_shift, rank bigint NOT NULL,
      problem jsonb NOT NULL, proposal jsonb NOT NULL, summary jsonb NOT NULL,
      CONSTRAINT ck_replacement_candidate_rank CHECK(rank>0),
      CONSTRAINT uq_replacement_candidate_rank UNIQUE(evaluation_id,rank),
      CONSTRAINT uq_replacement_candidate_shift UNIQUE(evaluation_id,shift_id))""")
    for table in ("replacement_evaluation", "replacement_candidate"):
        op.execute(
            f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()"
        )
    op.execute("""ALTER TABLE rider_replacement
      ADD COLUMN requested_by uuid REFERENCES app_user,
      ADD COLUMN reason varchar(100), ADD COLUMN incident_id uuid REFERENCES fleet_exception,
      ADD COLUMN evaluation_id uuid CONSTRAINT fk_replacement_evaluation
        REFERENCES replacement_evaluation DEFERRABLE INITIALLY DEFERRED,
      ADD COLUMN selected_candidate_id uuid CONSTRAINT fk_replacement_selected_candidate
        REFERENCES replacement_candidate,
      ADD COLUMN activated_at timestamptz,
      ADD COLUMN activation_id uuid REFERENCES route_activation""")
    op.execute("""ALTER TABLE rider_replacement_decision
      ADD COLUMN candidate_id uuid REFERENCES replacement_candidate,
      ADD COLUMN reason varchar(100)""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM replacement_evaluation) OR EXISTS(
       SELECT 1 FROM rider_replacement WHERE requested_by IS NOT NULL OR reason IS NOT NULL
         OR incident_id IS NOT NULL OR evaluation_id IS NOT NULL
         OR selected_candidate_id IS NOT NULL OR activated_at IS NOT NULL
         OR activation_id IS NOT NULL) OR EXISTS(
       SELECT 1 FROM rider_replacement_decision WHERE candidate_id IS NOT NULL
         OR reason IS NOT NULL) THEN
       RAISE EXCEPTION 'export adopted replacement choices before downgrade'; END IF; END $$""")
    op.execute(
        "ALTER TABLE rider_replacement_decision DROP COLUMN candidate_id, DROP COLUMN reason"
    )
    op.execute(
        "ALTER TABLE rider_replacement DROP COLUMN requested_by,DROP COLUMN reason,"
        "DROP COLUMN incident_id,DROP COLUMN evaluation_id,DROP COLUMN selected_candidate_id,"
        "DROP COLUMN activated_at,DROP COLUMN activation_id"
    )
    op.execute("DROP TABLE replacement_candidate,replacement_evaluation")
