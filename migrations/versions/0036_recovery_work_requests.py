"""Coalesced recovery generations and independent publication leases."""

from alembic import op

revision = "0036_recovery_work_requests"
down_revision = "0035_rider_failure_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM recovery_assignment WHERE lease_until > now()) OR
           EXISTS(SELECT 1 FROM rider_replacement WHERE recommendation_until > now()) THEN
            RAISE EXCEPTION 'drain recovery execution leases before work-generation cutover';
        END IF;
    END $$""")
    op.execute("""CREATE TABLE recovery_work_request (
        outbox_event_id uuid PRIMARY KEY REFERENCES outbox_event,
        assignment_id uuid REFERENCES recovery_assignment,
        replacement_id uuid REFERENCES rider_replacement,
        generation bigint NOT NULL, state varchar(24) NOT NULL,
        publication_token uuid, publication_until timestamptz,
        started_at timestamptz, closed_at timestamptz, close_reason varchar(80),
        CONSTRAINT ck_recovery_work_owner CHECK(
            (assignment_id IS NOT NULL) <> (replacement_id IS NOT NULL)),
        CONSTRAINT ck_recovery_work_generation CHECK(generation > 0),
        CONSTRAINT ck_recovery_work_state CHECK(
            state IN ('READY','PUBLISHING','PUBLISHED','RETRY_DUE','CLOSED')),
        CONSTRAINT ck_recovery_work_lease CHECK(
            (state = 'PUBLISHING') = (publication_token IS NOT NULL) AND
            (publication_token IS NULL) = (publication_until IS NULL)),
        CONSTRAINT ck_recovery_work_closed CHECK((state = 'CLOSED') = (closed_at IS NOT NULL)),
        CONSTRAINT uq_recovery_work_assignment_generation UNIQUE(assignment_id,generation),
        CONSTRAINT uq_recovery_work_replacement_generation UNIQUE(replacement_id,generation))""")
    for name in ("assignment", "replacement"):
        op.execute(
            f"CREATE UNIQUE INDEX uq_recovery_work_{name}_open ON "
            f"recovery_work_request({name}_id) WHERE state <> 'CLOSED'"
        )
    # Retain one real legacy delivery identity per actual operation; already processed
    # deliveries cannot block its next operational retry. Older broker copies become no-ops.
    op.execute("""WITH eligible AS (
        SELECT o.*, r.assignment_id, NULL::uuid AS replacement_id,
            r.status NOT IN ('PENDING','RUNNING','AWAITING_TRANSFER') OR
                (r.attempt > 0 AND r.lease_until IS NULL AND
                 r.status IN ('PENDING','AWAITING_TRANSFER')) AS terminal
        FROM outbox_event o JOIN recovery_assignment r ON r.assignment_id=o.aggregate_id
        WHERE o.event_type='RecoveryWorkRequested' AND o.payload->>'kind'='ACTIVATE'
        UNION ALL
        SELECT o.*, NULL::uuid, r.replacement_id,
            NOT (r.status='REQUESTED' OR
                (r.status='APPROVED' AND
                 r.failure_code IS NOT DISTINCT FROM 'RECOMMENDATION_REQUESTED')) OR
                (r.recommendation_until IS NULL AND r.next_attempt_at > o.created_at)
        FROM outbox_event o JOIN rider_replacement r ON r.replacement_id=o.aggregate_id
        WHERE o.event_type='RecoveryWorkRequested' AND o.payload->>'kind'='RECOMMEND'
    ), chosen AS (
        SELECT DISTINCT ON (aggregate_id,payload->>'kind') * FROM eligible
        ORDER BY aggregate_id,payload->>'kind',
            CASE WHEN status='PUBLISHED' AND NOT EXISTS(SELECT 1 FROM inbox_message i
                WHERE i.consumer_name='routing-recovery' AND i.status='PROCESSED'
                    AND i.message_id=eligible.outbox_event_id::text) THEN 0
                 WHEN status='PENDING' THEN 1 ELSE 2 END,
            created_at DESC,outbox_event_id DESC
    ), adopted AS (
        SELECT c.*, terminal OR EXISTS(SELECT 1 FROM inbox_message i
            WHERE i.consumer_name='routing-recovery' AND
                i.message_id=c.outbox_event_id::text AND i.status='PROCESSED') AS closed
        FROM chosen c
    )
    INSERT INTO recovery_work_request(outbox_event_id,assignment_id,replacement_id,
        generation,state,closed_at,close_reason)
    SELECT outbox_event_id,assignment_id,replacement_id,1,
        CASE WHEN closed THEN 'CLOSED' WHEN status='PUBLISHED' THEN 'PUBLISHED' ELSE 'READY' END,
        CASE WHEN closed THEN now() END, CASE WHEN closed THEN 'LEGACY_OUTCOME_RECORDED' END
    FROM adopted""")
    op.execute("""UPDATE outbox_event o SET status='CANCELLED'
        WHERE o.event_type='RecoveryWorkRequested' AND o.status='PENDING' AND NOT EXISTS(
            SELECT 1 FROM recovery_work_request w WHERE w.outbox_event_id=o.outbox_event_id
                AND w.state <> 'CLOSED')""")
    op.execute("""CREATE FUNCTION guard_recovery_work_identity() RETURNS trigger AS $$ BEGIN
        IF (NEW.outbox_event_id,NEW.assignment_id,NEW.replacement_id,NEW.generation)
           IS DISTINCT FROM
           (OLD.outbox_event_id,OLD.assignment_id,OLD.replacement_id,OLD.generation) THEN
            RAISE EXCEPTION 'recovery work identity is immutable';
        END IF;
        RETURN NEW;
    END $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER recovery_work_identity BEFORE UPDATE ON recovery_work_request
        FOR EACH ROW EXECUTE FUNCTION guard_recovery_work_identity()""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM recovery_work_request) THEN
            RAISE EXCEPTION 'export adopted recovery work requests before downgrade';
        END IF;
    END $$""")
    op.execute("DROP TABLE recovery_work_request")
    op.execute("DROP FUNCTION guard_recovery_work_identity()")
