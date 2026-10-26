"""Customer accounts, controlled content publication, owned notification history and delivery.

DDL is frozen here; migrations never import evolving application models.
"""

from alembic import op

revision = "0039_customer_capabilities"
down_revision = "0038_item_volume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """ALTER TABLE refresh_session ADD CONSTRAINT uq_refresh_session_owner UNIQUE
      (refresh_session_id,user_id)"""
    )
    op.execute(
        """ALTER TABLE collection_request ADD CONSTRAINT uq_collection_request_owner UNIQUE
      (request_id,customer_id)"""
    )
    op.execute("""CREATE TABLE customer_profile (
    user_id UUID NOT NULL,
    details_encrypted BYTEA,
    version BIGINT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (user_id),
    CONSTRAINT ck_customer_profile_version CHECK (version >= 1),
    FOREIGN KEY(user_id) REFERENCES app_user (user_id)
)""")
    op.execute("""CREATE TABLE customer_preferences (
    user_id UUID NOT NULL,
    language VARCHAR(8) NOT NULL,
    collection_notifications BOOLEAN NOT NULL,
    version BIGINT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (user_id),
    CONSTRAINT ck_customer_preferences_version CHECK (version >= 1),
    CONSTRAINT ck_customer_preferences_language CHECK (language IN ('en-IN', 'hi-IN')),
    FOREIGN KEY(user_id) REFERENCES app_user (user_id)
)""")
    op.execute("""CREATE TABLE customer_favourite_set (
    user_id UUID NOT NULL,
    version BIGINT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (user_id),
    CONSTRAINT ck_customer_favourite_version CHECK (version >= 1),
    FOREIGN KEY(user_id) REFERENCES app_user (user_id)
)""")
    op.execute("""CREATE TABLE customer_favourite_category (
    user_id UUID NOT NULL,
    category_id UUID NOT NULL,
    PRIMARY KEY (user_id, category_id),
    FOREIGN KEY(user_id) REFERENCES customer_favourite_set (user_id),
    FOREIGN KEY(category_id) REFERENCES catalogue_category (category_id)
)""")
    op.execute("""CREATE TABLE customer_command_receipt (
    receipt_id UUID NOT NULL,
    command_id UUID NOT NULL,
    user_id UUID NOT NULL,
    result_encrypted BYTEA NOT NULL,
    PRIMARY KEY (receipt_id),
    UNIQUE (command_id),
    FOREIGN KEY(command_id) REFERENCES idempotency_record (idempotency_record_id),
    FOREIGN KEY(user_id) REFERENCES app_user (user_id)
)""")
    op.execute(
        """CREATE INDEX ix_customer_command_receipt_user_id ON customer_command_receipt (user_id)"""
    )
    op.execute("""CREATE TABLE customer_feedback (
    feedback_id UUID NOT NULL,
    user_id UUID NOT NULL,
    client_request_id UUID NOT NULL,
    message_encrypted BYTEA NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (feedback_id),
    CONSTRAINT uq_customer_feedback_command UNIQUE (user_id, client_request_id),
    FOREIGN KEY(user_id) REFERENCES app_user (user_id)
)""")
    op.execute("""CREATE INDEX ix_customer_feedback_user_id ON customer_feedback (user_id)""")
    op.execute("""CREATE TABLE content_revision (
    revision_id UUID NOT NULL,
    slug VARCHAR(16) NOT NULL,
    revision INTEGER NOT NULL,
    title VARCHAR(200) NOT NULL,
    status VARCHAR(16) NOT NULL,
    authored_by UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    approved_by UUID,
    approval_reference VARCHAR(200),
    published_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (revision_id),
    CONSTRAINT uq_content_slug_revision UNIQUE (slug, revision),
    CONSTRAINT ck_content_slug CHECK (slug IN ('help','about','privacy','terms')),
    CONSTRAINT ck_content_revision CHECK (revision > 0),
    CONSTRAINT ck_content_status CHECK (status IN ('DRAFT','PUBLISHED','RETIRED')),
    CONSTRAINT ck_content_approval CHECK ((status = 'DRAFT' AND published_at IS NULL AND
      approved_by IS NULL AND approval_reference IS NULL) OR (status IN
      ('PUBLISHED','RETIRED') AND published_at IS NOT NULL AND approved_by IS NOT NULL AND
      approval_reference IS NOT NULL)),
    FOREIGN KEY(authored_by) REFERENCES app_user (user_id),
    FOREIGN KEY(approved_by) REFERENCES app_user (user_id)
)""")
    op.execute(
        """CREATE UNIQUE INDEX uq_content_published_slug ON content_revision (slug) WHERE status =
      'PUBLISHED'"""
    )
    op.execute("""CREATE TABLE content_paragraph (
    revision_id UUID NOT NULL,
    position INTEGER NOT NULL,
    body VARCHAR(4000) NOT NULL,
    PRIMARY KEY (revision_id, position),
    CONSTRAINT ck_content_paragraph_position CHECK (position >= 0),
    CONSTRAINT ck_content_paragraph_body CHECK (length(body) BETWEEN 1 AND 4000),
    FOREIGN KEY(revision_id) REFERENCES content_revision (revision_id)
)""")
    op.execute("""CREATE TABLE customer_push_binding (
    binding_id UUID NOT NULL,
    client_device_id UUID NOT NULL,
    user_id UUID NOT NULL,
    session_id UUID NOT NULL,
    platform VARCHAR(16) NOT NULL,
    provider VARCHAR(16) NOT NULL,
    browser_origin VARCHAR(300),
    token_hmac BYTEA NOT NULL,
    credential_hmac BYTEA NOT NULL,
    token_encrypted BYTEA NOT NULL,
    generation BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (binding_id),
    CONSTRAINT uq_customer_binding_owner UNIQUE (binding_id, user_id),
    CONSTRAINT fk_customer_binding_session_owner FOREIGN KEY(session_id, user_id) REFERENCES
      refresh_session (refresh_session_id, user_id),
    CONSTRAINT ck_customer_binding_platform CHECK ((platform = 'ANDROID' AND provider = 'FCM'
      AND browser_origin IS NULL) OR (platform = 'WEB' AND provider = 'WEBPUSH' AND
      browser_origin IS NOT NULL)),
    CONSTRAINT ck_customer_binding_generation CHECK (generation >= 1),
    FOREIGN KEY(user_id) REFERENCES app_user (user_id)
)""")
    op.execute(
        """CREATE INDEX ix_customer_binding_user ON customer_push_binding (user_id, session_id)"""
    )
    op.execute(
        """CREATE UNIQUE INDEX uq_customer_binding_installation ON customer_push_binding
      (client_device_id) WHERE revoked_at IS NULL"""
    )
    op.execute(
        """CREATE UNIQUE INDEX uq_customer_binding_token ON customer_push_binding (provider,
      token_hmac) WHERE revoked_at IS NULL"""
    )
    op.execute("""CREATE TABLE customer_notification (
    notification_id UUID NOT NULL,
    source_event_id UUID NOT NULL,
    user_id UUID NOT NULL,
    request_id UUID NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    title VARCHAR(200) NOT NULL,
    body VARCHAR(500) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (notification_id),
    CONSTRAINT uq_customer_notification_owner UNIQUE (notification_id, user_id),
    CONSTRAINT fk_customer_notification_request_owner FOREIGN KEY(request_id, user_id)
      REFERENCES collection_request (request_id, customer_id),
    UNIQUE (source_event_id),
    FOREIGN KEY(source_event_id) REFERENCES outbox_event (outbox_event_id),
    FOREIGN KEY(user_id) REFERENCES app_user (user_id)
)""")
    op.execute(
        """CREATE INDEX ix_customer_notification_page ON customer_notification (user_id,
      created_at, notification_id)"""
    )
    op.execute("""CREATE TABLE customer_push_delivery (
    delivery_id UUID NOT NULL,
    notification_id UUID NOT NULL,
    user_id UUID NOT NULL,
    binding_id UUID NOT NULL,
    binding_generation BIGINT NOT NULL,
    status VARCHAR(24) NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
    lease_token UUID,
    lease_until TIMESTAMP WITH TIME ZONE,
    failure_code VARCHAR(32),
    PRIMARY KEY (delivery_id),
    CONSTRAINT fk_customer_delivery_notification_owner FOREIGN KEY(notification_id, user_id)
      REFERENCES customer_notification (notification_id, user_id),
    CONSTRAINT fk_customer_delivery_binding_owner FOREIGN KEY(binding_id, user_id) REFERENCES
      customer_push_binding (binding_id, user_id),
    CONSTRAINT uq_customer_delivery_device UNIQUE (notification_id, binding_id),
    CONSTRAINT ck_customer_delivery_status CHECK (status IN
      ('PENDING','SENDING','SENT','SUPPRESSED','PERMANENTLY_FAILED')),
    CONSTRAINT ck_customer_delivery_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_customer_delivery_lease CHECK ((status = 'SENDING' AND lease_token IS NOT
      NULL AND lease_until IS NOT NULL) OR (status <> 'SENDING' AND lease_token IS NULL AND
      lease_until IS NULL)),
    FOREIGN KEY(user_id) REFERENCES app_user (user_id)
)""")
    op.execute(
        """CREATE INDEX ix_customer_delivery_due ON customer_push_delivery
      (status, next_attempt_at)"""
    )
    _guards()
    op.execute("CREATE INDEX ix_customer_binding_session ON customer_push_binding (session_id)")
    op.execute("""CREATE INDEX ix_customer_binding_installation_history ON customer_push_binding
      (client_device_id,session_id)""")
    op.execute("""CREATE INDEX ix_customer_binding_token_history ON customer_push_binding
      (provider,token_hmac,session_id)""")
    op.execute("""CREATE INDEX ix_customer_binding_credential_history ON customer_push_binding
      (provider,credential_hmac,session_id)""")
    op.execute("""CREATE INDEX ix_customer_event_fanout ON outbox_event (created_at,outbox_event_id)
      WHERE event_type IN ('CollectionRequestAccepted','CustomerFinancialStatusChanged')""")


def _guards() -> None:
    op.execute("""CREATE FUNCTION customer_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='DELETE' OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR
          (to_jsonb(OLD)-'revoked_at') IS DISTINCT FROM (to_jsonb(NEW)-'revoked_at') THEN
          RAISE EXCEPTION 'immutable customer push binding';
        END IF; RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER customer_binding_guard BEFORE UPDATE OR DELETE ON
      customer_push_binding FOR EACH ROW EXECUTE FUNCTION customer_binding_guard()""")
    op.execute("""CREATE FUNCTION customer_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'immutable customer history'; END $$""")
    for table in ("customer_notification", "customer_feedback", "customer_command_receipt"):
        op.execute(
            f"CREATE TRIGGER customer_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION customer_immutable()"
        )
    op.execute("""CREATE FUNCTION customer_revoke_session_push() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.revoked_at IS NOT NULL OR NEW.expires_at <= clock_timestamp() THEN
          UPDATE customer_push_binding SET revoked_at=clock_timestamp()
          WHERE session_id=NEW.refresh_session_id AND revoked_at IS NULL;
        END IF; RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER customer_revoke_session_push AFTER UPDATE ON refresh_session
      FOR EACH ROW EXECUTE FUNCTION customer_revoke_session_push()""")
    op.execute("""CREATE FUNCTION customer_content_guard() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable content history'; END IF;
        IF TG_OP = 'INSERT' THEN
          IF NEW.status <> 'DRAFT' THEN RAISE EXCEPTION 'content must start as draft'; END IF;
          RETURN NEW;
        END IF;
        IF
      (to_jsonb(NEW)-ARRAY['status','approved_by','approval_reference','published_at']) IS
      DISTINCT
          FROM
      (to_jsonb(OLD)-ARRAY['status','approved_by','approval_reference','published_at']) THEN
          RAISE EXCEPTION 'immutable content revision';
        END IF;
        IF OLD.status='DRAFT' AND NEW.status='PUBLISHED' AND EXISTS(
          SELECT 1 FROM content_paragraph WHERE revision_id=NEW.revision_id) THEN RETURN
      NEW; END IF;
        IF OLD.status='PUBLISHED' AND NEW.status='RETIRED' AND
          (NEW.approved_by,NEW.approval_reference,NEW.published_at) IS NOT DISTINCT FROM
          (OLD.approved_by,OLD.approval_reference,OLD.published_at) THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'invalid content transition'; END $$""")
    op.execute("""CREATE TRIGGER customer_content_guard BEFORE INSERT OR UPDATE OR DELETE ON
      content_revision FOR EACH ROW EXECUTE FUNCTION customer_content_guard()""")
    op.execute("""CREATE FUNCTION customer_paragraph_guard() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable paragraph'; END IF;
        PERFORM 1 FROM content_revision WHERE revision_id=NEW.revision_id AND status='DRAFT'
          FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'paragraph requires draft'; END IF;
        RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER customer_paragraph_guard BEFORE INSERT OR UPDATE OR DELETE ON
      content_paragraph FOR EACH ROW EXECUTE FUNCTION customer_paragraph_guard()""")
    op.execute("""CREATE FUNCTION customer_notification_source() RETURNS trigger
      LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM 1 FROM outbox_event WHERE outbox_event_id=NEW.source_event_id
          AND aggregate_type='collection_request' AND aggregate_id=NEW.request_id
          AND event_type=NEW.event_type AND event_type IN
          ('CollectionRequestAccepted','CustomerFinancialStatusChanged');
        IF NOT FOUND THEN RAISE EXCEPTION 'invalid customer notification source'; END IF;
        RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER customer_notification_source BEFORE INSERT ON customer_notification
      FOR EACH ROW EXECUTE FUNCTION customer_notification_source()""")


def downgrade() -> None:
    # Refuse silent destruction of private data, replay promises and publication/event history.
    for table in (
        "customer_profile",
        "customer_preferences",
        "customer_favourite_set",
        "customer_favourite_category",
        "customer_command_receipt",
        "customer_feedback",
        "content_revision",
        "content_paragraph",
        "customer_push_binding",
        "customer_notification",
        "customer_push_delivery",
    ):
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {table}) THEN "
            "RAISE EXCEPTION 'export customer capability data before downgrade'; END IF; END $$"
        )
    op.execute("DROP TRIGGER customer_revoke_session_push ON refresh_session")
    op.execute("DROP INDEX IF EXISTS ix_customer_event_fanout")
    for table in reversed(
        (
            "customer_profile",
            "customer_preferences",
            "customer_favourite_set",
            "customer_favourite_category",
            "customer_command_receipt",
            "customer_feedback",
            "content_revision",
            "content_paragraph",
            "customer_push_binding",
            "customer_notification",
            "customer_push_delivery",
        )
    ):
        op.drop_table(table)
    for name in (
        "customer_immutable",
        "customer_revoke_session_push",
        "customer_content_guard",
        "customer_paragraph_guard",
        "customer_notification_source",
        "customer_binding_guard",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {name}()")
    op.drop_constraint("uq_refresh_session_owner", "refresh_session", type_="unique")
    op.drop_constraint("uq_collection_request_owner", "collection_request", type_="unique")
