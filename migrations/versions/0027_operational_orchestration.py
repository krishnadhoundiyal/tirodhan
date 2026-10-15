"""Regional admission, orchestration and explicit operational recovery history."""

from alembic import op

revision = "0027_operational_orchestration"
down_revision = "0026_continuous_fleet"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
ALTER TABLE operating_vehicle ADD COLUMN safety_version BIGINT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
CREATE INDEX ix_outbox_routing_wakeup ON outbox_event (available_at,outbox_event_id) WHERE
event_type='CustomerFinancialStatusChanged'
        """
    )
    op.execute(
        """
CREATE TABLE routing_fleet_wakeup (
    scope VARCHAR(100) NOT NULL,
    generation BIGINT DEFAULT '1' NOT NULL,
    acknowledged BIGINT DEFAULT '0' NOT NULL,
    due_at TIMESTAMP WITH TIME ZONE NOT NULL,
    reason VARCHAR(50) NOT NULL,
    PRIMARY KEY (scope),
    CONSTRAINT ck_wakeup_generation CHECK (generation >= acknowledged AND acknowledged >=
0)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_routing_fleet_wakeup_due_at ON routing_fleet_wakeup (due_at)
        """
    )
    op.execute(
        """
CREATE TABLE operating_region (
    region_id UUID NOT NULL,
    name VARCHAR(100) NOT NULL,
    default_warehouse_id UUID NOT NULL,
    version BIGINT DEFAULT '1' NOT NULL,
    policy JSONB NOT NULL,
    recorded_by UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (region_id),
    CONSTRAINT ck_region_version CHECK (version > 0),
    UNIQUE (name),
    FOREIGN KEY(default_warehouse_id) REFERENCES receiving_point (receiving_point_id),
    FOREIGN KEY(recorded_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        """
CREATE TABLE operating_region_cell (
    cell_id VARCHAR(100) NOT NULL,
    region_id UUID NOT NULL,
    PRIMARY KEY (cell_id),
    FOREIGN KEY(region_id) REFERENCES operating_region (region_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_operating_region_cell_region_id ON operating_region_cell (region_id)
        """
    )
    op.execute(
        """
CREATE TABLE operating_region_receiving_point (
    region_id UUID NOT NULL,
    receiving_point_id UUID NOT NULL,
    PRIMARY KEY (region_id, receiving_point_id),
    FOREIGN KEY(region_id) REFERENCES operating_region (region_id),
    FOREIGN KEY(receiving_point_id) REFERENCES receiving_point (receiving_point_id)
)
        """
    )
    op.execute(
        """
CREATE TABLE operating_region_vehicle (
    vehicle_id UUID NOT NULL,
    region_id UUID NOT NULL,
    PRIMARY KEY (vehicle_id),
    FOREIGN KEY(vehicle_id) REFERENCES operating_vehicle (vehicle_id),
    FOREIGN KEY(region_id) REFERENCES operating_region (region_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_operating_region_vehicle_region_id ON operating_region_vehicle (region_id)
        """
    )
    op.execute(
        """
CREATE TABLE operating_break (
    break_id UUID NOT NULL,
    shift_id UUID NOT NULL,
    starts_at TIMESTAMP WITH TIME ZONE NOT NULL,
    ends_at TIMESTAMP WITH TIME ZONE NOT NULL,
    recorded_by UUID NOT NULL,
    PRIMARY KEY (break_id),
    CONSTRAINT ck_break_window CHECK (ends_at > starts_at),
    FOREIGN KEY(shift_id) REFERENCES operating_shift (shift_id),
    FOREIGN KEY(recorded_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_operating_break_shift_id ON operating_break (shift_id)
        """
    )
    op.execute(
        """
CREATE TABLE operating_shift_extension (
    extension_id UUID NOT NULL,
    shift_id UUID NOT NULL,
    ends_at TIMESTAMP WITH TIME ZONE NOT NULL,
    rider_confirmed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    authorized_by UUID NOT NULL,
    authorized_at TIMESTAMP WITH TIME ZONE NOT NULL,
    safe_conditions_confirmed BOOLEAN DEFAULT 'true' NOT NULL,
    PRIMARY KEY (extension_id),
    FOREIGN KEY(shift_id) REFERENCES operating_shift (shift_id),
    FOREIGN KEY(authorized_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_operating_shift_extension_shift_id ON operating_shift_extension (shift_id)
        """
    )
    op.execute(
        """
CREATE TABLE shift_extension_consent (
    consent_id UUID NOT NULL,
    shift_id UUID NOT NULL,
    rider_id UUID NOT NULL,
    ends_at TIMESTAMP WITH TIME ZONE NOT NULL,
    consented_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (consent_id),
    FOREIGN KEY(shift_id) REFERENCES operating_shift (shift_id),
    FOREIGN KEY(rider_id) REFERENCES rider_profile (rider_id)
)
        """
    )
    op.execute(
        """
CREATE TABLE capacity_admission_request (
    admission_id UUID NOT NULL,
    reattempt_request_id UUID,
    customer_id UUID NOT NULL,
    context_id UUID NOT NULL,
    slot_start TIMESTAMP WITH TIME ZONE NOT NULL,
    slot_end TIMESTAMP WITH TIME ZONE NOT NULL,
    mass_g BIGINT NOT NULL,
    volume_ml BIGINT NOT NULL,
    pickup_seconds INTEGER NOT NULL,
    discharge_seconds INTEGER NOT NULL,
    fingerprint BYTEA NOT NULL,
    status VARCHAR(16) NOT NULL,
    failure_code VARCHAR(100),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    lease_token UUID,
    lease_until TIMESTAMP WITH TIME ZONE,
    attempts INTEGER DEFAULT '0' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (admission_id),
    CONSTRAINT ck_admission_status CHECK (status IN
('PENDING','RUNNING','SUCCEEDED','FAILED','NEEDS_ACCEPTANCE')),
    CONSTRAINT ck_admission_inputs CHECK (slot_end > slot_start AND expires_at >
created_at AND mass_g > 0 AND volume_ml > 0 AND pickup_seconds >= 0 AND discharge_seconds
>= 0),
    FOREIGN KEY(reattempt_request_id) REFERENCES collection_request (request_id),
    FOREIGN KEY(customer_id) REFERENCES app_user (user_id),
    FOREIGN KEY(context_id) REFERENCES serviceability_context (serviceability_context_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_capacity_admission_due ON capacity_admission_request (status, created_at,
admission_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_capacity_admission_lease ON capacity_admission_request (status,
lease_until)
        """
    )
    op.execute(
        """
CREATE TABLE cargo_transfer_intent (
    intent_id UUID NOT NULL,
    request_id UUID NOT NULL,
    from_vehicle_id UUID NOT NULL,
    to_shift_id UUID NOT NULL,
    authorized_by UUID NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    resolved_at TIMESTAMP WITH TIME ZONE,
    resolution VARCHAR(16),
    resolved_by UUID,
    resolution_evidence_id UUID,
    PRIMARY KEY (intent_id),
    CONSTRAINT ck_transfer_resolution CHECK ((resolved_at IS NULL AND resolution IS NULL
AND resolved_by IS NULL AND resolution_evidence_id IS NULL) OR (resolved_at IS NOT NULL
AND resolved_by IS NOT NULL AND resolution IS NOT NULL AND (resolution='TRANSFERRED' OR
(resolution='ABORTED' AND resolution_evidence_id IS NOT NULL)))),
    FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
    FOREIGN KEY(from_vehicle_id) REFERENCES operating_vehicle (vehicle_id),
    FOREIGN KEY(to_shift_id) REFERENCES operating_shift (shift_id),
    FOREIGN KEY(authorized_by) REFERENCES app_user (user_id),
    FOREIGN KEY(resolved_by) REFERENCES app_user (user_id),
    FOREIGN KEY(resolution_evidence_id) REFERENCES evidence_capture (evidence_capture_id)
)
        """
    )
    op.execute(
        """
CREATE UNIQUE INDEX uq_open_cargo_transfer ON cargo_transfer_intent (request_id) WHERE
resolved_at IS NULL
        """
    )
    op.execute(
        """
CREATE TABLE discharge_authorization (
    authorization_id UUID NOT NULL,
    request_id UUID NOT NULL,
    receiving_point_id UUID NOT NULL,
    discharge_deadline TIMESTAMP WITH TIME ZONE NOT NULL,
    authorized_by UUID NOT NULL,
    authorized_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (authorization_id),
    FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
    FOREIGN KEY(receiving_point_id) REFERENCES receiving_point (receiving_point_id),
    FOREIGN KEY(authorized_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_discharge_authorization_request_id ON discharge_authorization (request_id)
        """
    )
    op.execute(
        """
CREATE TABLE flexible_route_offer (
    offer_id UUID NOT NULL,
    region_id UUID NOT NULL,
    shift_id UUID NOT NULL,
    request_id UUID NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    accepted_at TIMESTAMP WITH TIME ZONE,
    accepted_by UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (offer_id),
    FOREIGN KEY(region_id) REFERENCES operating_region (region_id),
    FOREIGN KEY(shift_id) REFERENCES operating_shift (shift_id),
    FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
    FOREIGN KEY(accepted_by) REFERENCES rider_profile (rider_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_flexible_route_offer_request_id ON flexible_route_offer (request_id)
        """
    )
    op.execute(
        """
CREATE TABLE physical_load_observation (
    observation_id UUID NOT NULL,
    request_id UUID NOT NULL,
    mass_g BIGINT NOT NULL,
    volume_ml BIGINT NOT NULL,
    capture_id UUID NOT NULL,
    recorded_by UUID NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    evidence_media_type VARCHAR(16) DEFAULT 'PHOTO' NOT NULL,
    PRIMARY KEY (observation_id),
    CONSTRAINT ck_observed_load CHECK (mass_g > 0 AND volume_ml > 0 AND
evidence_media_type = 'PHOTO'),
    FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
    UNIQUE (capture_id),
    FOREIGN KEY(capture_id) REFERENCES evidence_capture (evidence_capture_id),
    FOREIGN KEY(recorded_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_physical_load_observation_request_id ON physical_load_observation
(request_id)
        """
    )
    op.execute(
        """
CREATE TABLE regional_capacity_reservation (
    reservation_id UUID NOT NULL,
    region_id UUID NOT NULL,
    region_version BIGINT NOT NULL,
    customer_id UUID NOT NULL,
    context_id UUID NOT NULL,
    request_id UUID,
    pickup_location geography(POINT,4326) NOT NULL,
    slot_start TIMESTAMP WITH TIME ZONE NOT NULL,
    slot_end TIMESTAMP WITH TIME ZONE NOT NULL,
    mass_g BIGINT NOT NULL,
    volume_ml BIGINT NOT NULL,
    pickup_seconds INTEGER NOT NULL,
    discharge_seconds INTEGER NOT NULL,
    receiving_point_id UUID NOT NULL,
    discharge_deadline TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    fingerprint BYTEA NOT NULL,
    certificate JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (reservation_id),
    CONSTRAINT uq_region_capacity_version UNIQUE (region_id, region_version),
    CONSTRAINT ck_capacity_region_version CHECK (region_version > 0),
    CONSTRAINT ck_reservation_times CHECK (slot_end > slot_start AND expires_at >
created_at),
    CONSTRAINT ck_reservation_load CHECK (mass_g > 0 AND volume_ml > 0 AND pickup_seconds
>= 0 AND discharge_seconds >= 0),
    FOREIGN KEY(region_id) REFERENCES operating_region (region_id),
    FOREIGN KEY(customer_id) REFERENCES app_user (user_id),
    FOREIGN KEY(context_id) REFERENCES serviceability_context (serviceability_context_id),
    FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
    FOREIGN KEY(receiving_point_id) REFERENCES receiving_point (receiving_point_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_regional_capacity_reservation_expires_at ON regional_capacity_reservation
(expires_at)
        """
    )
    op.execute(
        """
CREATE INDEX ix_regional_capacity_reservation_region_id ON regional_capacity_reservation
(region_id)
        """
    )
    op.execute(
        """
CREATE INDEX ix_regional_capacity_reservation_request_id ON regional_capacity_reservation
(request_id)
        """
    )
    op.execute(
        """
CREATE TABLE admission_flexible_offer (
    offer_id UUID NOT NULL,
    admission_id UUID NOT NULL,
    shift_id UUID NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (offer_id),
    CONSTRAINT uq_admission_flexible_offer UNIQUE (admission_id, shift_id),
    FOREIGN KEY(admission_id) REFERENCES capacity_admission_request (admission_id),
    FOREIGN KEY(shift_id) REFERENCES operating_shift (shift_id)
)
        """
    )
    op.execute(
        """
CREATE TABLE appointment_consent (
    consent_id UUID NOT NULL,
    request_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    slot_start TIMESTAMP WITH TIME ZONE NOT NULL,
    slot_end TIMESTAMP WITH TIME ZONE NOT NULL,
    reservation_id UUID NOT NULL,
    consented_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (consent_id),
    CONSTRAINT ck_consent_window CHECK (slot_end > slot_start),
    FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
    FOREIGN KEY(customer_id) REFERENCES app_user (user_id),
    UNIQUE (reservation_id),
    FOREIGN KEY(reservation_id) REFERENCES regional_capacity_reservation (reservation_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_appointment_consent_request_id ON appointment_consent (request_id)
        """
    )
    op.execute(
        """
CREATE TABLE cargo_transfer (
    transfer_id UUID NOT NULL,
    intent_id UUID NOT NULL,
    sequence INTEGER NOT NULL,
    request_id UUID NOT NULL,
    from_vehicle_id UUID NOT NULL,
    to_vehicle_id UUID NOT NULL,
    from_rider_id UUID NOT NULL,
    to_rider_id UUID NOT NULL,
    donor_evidence_id UUID NOT NULL,
    receiver_evidence_id UUID NOT NULL,
    authorized_by UUID NOT NULL,
    confirmed_by UUID NOT NULL,
    transferred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (transfer_id),
    CONSTRAINT ck_cargo_transfer_resources CHECK (from_vehicle_id <> to_vehicle_id AND
from_rider_id <> to_rider_id AND sequence > 0),
    CONSTRAINT uq_cargo_transfer_sequence UNIQUE (request_id, sequence),
    UNIQUE (intent_id),
    FOREIGN KEY(intent_id) REFERENCES cargo_transfer_intent (intent_id),
    FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
    FOREIGN KEY(from_vehicle_id) REFERENCES operating_vehicle (vehicle_id),
    FOREIGN KEY(to_vehicle_id) REFERENCES operating_vehicle (vehicle_id),
    FOREIGN KEY(from_rider_id) REFERENCES rider_profile (rider_id),
    FOREIGN KEY(to_rider_id) REFERENCES rider_profile (rider_id),
    FOREIGN KEY(donor_evidence_id) REFERENCES evidence_capture (evidence_capture_id),
    FOREIGN KEY(receiver_evidence_id) REFERENCES evidence_capture (evidence_capture_id),
    FOREIGN KEY(authorized_by) REFERENCES app_user (user_id),
    FOREIGN KEY(confirmed_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_cargo_transfer_request_id ON cargo_transfer (request_id)
        """
    )
    op.execute(
        """
CREATE TABLE cargo_transfer_receipt (
    intent_id UUID NOT NULL,
    rider_id UUID NOT NULL,
    capture_id UUID NOT NULL,
    witnessed_by UUID,
    evidence_media_type VARCHAR(16) DEFAULT 'PHOTO' NOT NULL,
    PRIMARY KEY (intent_id, rider_id),
    CONSTRAINT ck_transfer_receipt_photo CHECK (evidence_media_type = 'PHOTO'),
    FOREIGN KEY(intent_id) REFERENCES cargo_transfer_intent (intent_id),
    FOREIGN KEY(rider_id) REFERENCES rider_profile (rider_id),
    UNIQUE (capture_id),
    FOREIGN KEY(capture_id) REFERENCES evidence_capture (evidence_capture_id),
    FOREIGN KEY(witnessed_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        """
CREATE TABLE customer_route_eta (
    request_id UUID NOT NULL,
    revision_id UUID NOT NULL,
    rider_id UUID NOT NULL,
    service_start_at TIMESTAMP WITH TIME ZONE NOT NULL,
    calculated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    fresh_until TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (request_id),
    FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
    FOREIGN KEY(revision_id) REFERENCES route_revision (revision_id),
    FOREIGN KEY(rider_id) REFERENCES rider_profile (rider_id)
)
        """
    )
    op.execute(
        """
CREATE TABLE flexible_commitment (
    request_id UUID NOT NULL,
    shift_id UUID NOT NULL,
    offer_id UUID NOT NULL,
    accepted_by UUID NOT NULL,
    accepted_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (request_id),
    FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
    FOREIGN KEY(shift_id) REFERENCES operating_shift (shift_id),
    UNIQUE (offer_id),
    FOREIGN KEY(offer_id) REFERENCES flexible_route_offer (offer_id),
    FOREIGN KEY(accepted_by) REFERENCES rider_profile (rider_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_flexible_commitment_shift_id ON flexible_commitment (shift_id)
        """
    )
    op.execute(
        """
CREATE TABLE route_acknowledgment (
    revision_id UUID NOT NULL,
    rider_id UUID NOT NULL,
    command_id UUID NOT NULL,
    acknowledged_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (revision_id),
    FOREIGN KEY(revision_id) REFERENCES route_revision (revision_id),
    FOREIGN KEY(rider_id) REFERENCES rider_profile (rider_id),
    UNIQUE (command_id)
)
        """
    )
    op.execute(
        """
CREATE TABLE route_delivery (
    revision_id UUID NOT NULL,
    rider_id UUID NOT NULL,
    due_at TIMESTAMP WITH TIME ZONE NOT NULL,
    acknowledgment_due_at TIMESTAMP WITH TIME ZONE NOT NULL,
    attempts INTEGER DEFAULT '0' NOT NULL,
    escalated_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (revision_id),
    FOREIGN KEY(revision_id) REFERENCES route_revision (revision_id),
    FOREIGN KEY(rider_id) REFERENCES rider_profile (rider_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_route_delivery_due_at ON route_delivery (due_at)
        """
    )
    op.execute(
        """
CREATE INDEX ix_route_delivery_rider_id ON route_delivery (rider_id)
        """
    )
    op.execute(
        """
CREATE TABLE route_resume_authorization (
    authorization_id UUID NOT NULL,
    revision_id UUID NOT NULL,
    vehicle_safety_version BIGINT NOT NULL,
    authorized_by UUID NOT NULL,
    authorized_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (authorization_id),
    FOREIGN KEY(revision_id) REFERENCES route_revision (revision_id),
    FOREIGN KEY(authorized_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_route_resume_authorization_revision_id ON route_resume_authorization
(revision_id)
        """
    )
    op.execute(
        """
CREATE TABLE admission_flexible_commitment (
    admission_id UUID NOT NULL,
    offer_id UUID NOT NULL,
    shift_id UUID NOT NULL,
    accepted_by UUID NOT NULL,
    accepted_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (admission_id),
    FOREIGN KEY(admission_id) REFERENCES capacity_admission_request (admission_id),
    UNIQUE (offer_id),
    FOREIGN KEY(offer_id) REFERENCES admission_flexible_offer (offer_id),
    FOREIGN KEY(shift_id) REFERENCES operating_shift (shift_id),
    FOREIGN KEY(accepted_by) REFERENCES rider_profile (rider_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_admission_flexible_commitment_shift_id ON admission_flexible_commitment
(shift_id)
        """
    )
    op.execute(
        """
CREATE TABLE pickup_reattempt (
    attempt_id UUID NOT NULL,
    reason VARCHAR(30) NOT NULL,
    evidence_capture_id UUID NOT NULL,
    eligible_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (attempt_id),
    CONSTRAINT ck_reattempt_reason CHECK (reason IN
('CUSTOMER_UNAVAILABLE','UNCONTACTABLE','ACCESS_BLOCKED')),
    FOREIGN KEY(attempt_id) REFERENCES pickup_attempt (pickup_attempt_id),
    FOREIGN KEY(evidence_capture_id) REFERENCES evidence_capture (evidence_capture_id)
)
        """
    )
    op.execute(
        """
CREATE TABLE reattempt_authorization (
    authorization_id UUID NOT NULL,
    request_id UUID NOT NULL,
    after_attempt_id UUID NOT NULL,
    authorized_by UUID NOT NULL,
    authorized_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (authorization_id),
    FOREIGN KEY(request_id) REFERENCES collection_request (request_id),
    FOREIGN KEY(after_attempt_id) REFERENCES pickup_attempt (pickup_attempt_id),
    FOREIGN KEY(authorized_by) REFERENCES app_user (user_id)
)
        """
    )
    op.execute(
        """
CREATE INDEX ix_reattempt_authorization_request_id ON reattempt_authorization (request_id)
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
admission_flexible_commitment FOR EACH ROW EXECUTE FUNCTION
reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
appointment_consent FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON cargo_transfer
FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
cargo_transfer_receipt FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
discharge_authorization FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
flexible_commitment FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON operating_break
FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
operating_region_cell FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
operating_region_receiving_point FOR EACH ROW EXECUTE FUNCTION
reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
operating_region_vehicle FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
operating_shift_extension FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
physical_load_observation FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON pickup_reattempt
FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
reattempt_authorization FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
route_acknowledgment FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
route_resume_authorization FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE TRIGGER orchestration_history_immutable BEFORE UPDATE OR DELETE ON
shift_extension_consent FOR EACH ROW EXECUTE FUNCTION reject_route_history_mutation()
        """
    )
    op.execute(
        """
CREATE FUNCTION guard_capacity_reservation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
IF TG_OP = 'DELETE' THEN
RAISE EXCEPTION 'capacity history is retained' USING ERRCODE='23514'; END IF;
IF (to_jsonb(NEW)-'request_id') IS DISTINCT FROM (to_jsonb(OLD)-'request_id') OR
(OLD.request_id IS NOT NULL AND NEW.request_id IS DISTINCT FROM OLD.request_id) THEN
RAISE EXCEPTION 'capacity certificate is immutable and binds once' USING ERRCODE='23514';
END IF;
RETURN NEW; END $$
        """
    )
    op.execute(
        """
CREATE TRIGGER capacity_reservation_binding BEFORE UPDATE OR DELETE ON
regional_capacity_reservation
FOR EACH ROW EXECUTE FUNCTION guard_capacity_reservation()
        """
    )
    op.execute(
        """
CREATE OR REPLACE FUNCTION enforce_vehicle_route_binding() RETURNS trigger LANGUAGE
plpgsql AS $$
      BEGIN
        IF TG_OP = 'UPDATE' AND
          ROW(NEW.route_id, NEW.vehicle_id, NEW.shift_id, NEW.assignment_id) IS DISTINCT
FROM
          ROW(OLD.route_id, OLD.vehicle_id, OLD.shift_id, OLD.assignment_id) THEN
          RAISE EXCEPTION 'route binding is immutable' USING ERRCODE = '23514';
        END IF;
        IF NOT EXISTS (
          SELECT 1 FROM operating_shift s JOIN rider_assignment a ON a.rider_id =
s.rider_id
          WHERE s.shift_id = NEW.shift_id AND s.vehicle_id = NEW.vehicle_id
          AND (s.resource_kind = 'COMMITTED' OR EXISTS (
 SELECT 1 FROM flexible_commitment fc WHERE fc.shift_id = s.shift_id
 AND fc.accepted_by = s.rider_id) OR EXISTS (SELECT 1 FROM admission_flexible_commitment
ac WHERE ac.shift_id=s.shift_id AND ac.accepted_by=s.rider_id) OR EXISTS (SELECT 1 FROM
cargo_transfer ct WHERE ct.to_vehicle_id=s.vehicle_id AND ct.to_rider_id=s.rider_id)) AND
a.assignment_id = NEW.assignment_id
          AND a.source = 'COMMITTED_DISPATCH'
        ) THEN RAISE EXCEPTION 'invalid vehicle/shift/assignment binding' USING ERRCODE =
'23514';
        END IF;
        IF TG_OP = 'UPDATE' AND OLD.status = 'CLOSED' AND NEW IS DISTINCT FROM OLD THEN
          RAISE EXCEPTION 'closed route is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
      END $$;
        """
    )
    op.execute(
        """
CREATE FUNCTION guard_cargo_transfer() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE original_vehicle UUID; original_rider UUID; target_mass BIGINT; target_volume
BIGINT;
BEGIN
PERFORM 1 FROM collection_request WHERE request_id=NEW.request_id FOR UPDATE;
PERFORM 1 FROM operating_vehicle WHERE vehicle_id IN
(NEW.from_vehicle_id,NEW.to_vehicle_id)
ORDER BY vehicle_id FOR UPDATE;
IF NEW.sequence <> COALESCE((SELECT MAX(sequence) FROM cargo_transfer
WHERE request_id=NEW.request_id),0)+1 THEN
RAISE EXCEPTION 'invalid custody sequence' USING ERRCODE='23514'; END IF;
SELECT to_vehicle_id,to_rider_id INTO original_vehicle,original_rider FROM cargo_transfer
WHERE request_id=NEW.request_id ORDER BY sequence DESC LIMIT 1;
IF original_vehicle IS NULL THEN
SELECT cv.vehicle_id,ra.rider_id INTO original_vehicle,original_rider
FROM cargo_vehicle_observation cv JOIN pickup_attempt pa ON
pa.pickup_attempt_id=cv.pickup_attempt_id
JOIN rider_assignment ra ON ra.assignment_id=pa.rider_assignment_id WHERE
cv.request_id=NEW.request_id;
END IF;
IF original_vehicle IS DISTINCT FROM NEW.from_vehicle_id OR
original_rider IS DISTINCT FROM NEW.from_rider_id OR NOT EXISTS (
 SELECT 1 FROM pickup_execution pe WHERE pe.request_id=NEW.request_id AND
pe.status='COLLECTED'
 AND NOT EXISTS (SELECT 1 FROM handover_event_item hi WHERE
 hi.pickup_execution_id=pe.pickup_execution_id AND hi.status='VALIDATED')) THEN
RAISE EXCEPTION 'invalid current custody' USING ERRCODE='23514'; END IF;
IF NOT EXISTS (SELECT 1 FROM cargo_transfer_intent ci JOIN operating_shift os
 ON os.shift_id=ci.to_shift_id WHERE ci.intent_id=NEW.intent_id AND
ci.request_id=NEW.request_id
 AND ci.from_vehicle_id=NEW.from_vehicle_id AND os.vehicle_id=NEW.to_vehicle_id
 AND os.rider_id=NEW.to_rider_id AND ci.authorized_by=NEW.authorized_by AND ci.resolved_at
IS NULL) THEN
RAISE EXCEPTION 'transfer authorization mismatch' USING ERRCODE='23514'; END IF;
IF NOT EXISTS (SELECT 1 FROM cargo_transfer_receipt r JOIN evidence_capture e
 ON e.evidence_capture_id=r.capture_id
 WHERE r.intent_id=NEW.intent_id AND r.rider_id=NEW.from_rider_id
 AND r.capture_id=NEW.donor_evidence_id AND (e.captured_by_user_id=r.rider_id OR
(r.rider_id=NEW.from_rider_id AND e.captured_by_user_id=r.witnessed_by AND EXISTS (SELECT
1 FROM user_role u WHERE u.user_id=r.witnessed_by AND u.role_code='MANAGER' AND
u.granted_at<=e.created_at AND (u.revoked_at IS NULL OR u.revoked_at>=e.created_at)))) AND
r.evidence_media_type='PHOTO') OR
 NOT EXISTS (SELECT 1 FROM cargo_transfer_receipt r JOIN evidence_capture e
 ON e.evidence_capture_id=r.capture_id
 WHERE r.intent_id=NEW.intent_id AND r.rider_id=NEW.to_rider_id
 AND r.capture_id=NEW.receiver_evidence_id AND (e.captured_by_user_id=r.rider_id OR
(r.rider_id=NEW.from_rider_id AND e.captured_by_user_id=r.witnessed_by AND EXISTS (SELECT
1 FROM user_role u WHERE u.user_id=r.witnessed_by AND u.role_code='MANAGER' AND
u.granted_at<=e.created_at AND (u.revoked_at IS NULL OR u.revoked_at>=e.created_at)))) AND
r.evidence_media_type='PHOTO') THEN
RAISE EXCEPTION 'both transfer photo receipts required' USING ERRCODE='23514'; END IF;
SELECT COALESCE(SUM(COALESCE((SELECT po.mass_g FROM physical_load_observation po WHERE
po.request_id=m.request_id ORDER BY po.observed_at DESC,po.observation_id DESC LIMIT
1),m.mass_g)),0),COALESCE(SUM(COALESCE((SELECT po.volume_ml FROM physical_load_observation
po WHERE po.request_id=m.request_id ORDER BY po.observed_at DESC,po.observation_id DESC
LIMIT 1),m.volume_ml)),0) INTO target_mass,target_volume
FROM routing_manifest m JOIN cargo_vehicle_observation cv ON cv.request_id=m.request_id
JOIN pickup_execution pe ON pe.request_id=m.request_id
WHERE COALESCE((SELECT ct.to_vehicle_id FROM cargo_transfer ct WHERE
ct.request_id=m.request_id
 ORDER BY ct.sequence DESC LIMIT 1),cv.vehicle_id)=NEW.to_vehicle_id
AND NOT EXISTS (SELECT 1 FROM handover_event_item hi WHERE
hi.pickup_execution_id=pe.pickup_execution_id
 AND hi.status='VALIDATED');
IF NOT EXISTS (SELECT 1 FROM operating_vehicle v JOIN routing_manifest m ON
m.request_id=NEW.request_id
 WHERE v.vehicle_id=NEW.to_vehicle_id AND v.blocked_reason IS NULL
 AND target_mass+COALESCE((SELECT po.mass_g FROM physical_load_observation po WHERE
po.request_id=m.request_id ORDER BY po.observed_at DESC,po.observation_id DESC LIMIT
1),m.mass_g)<=v.mass_capacity_g AND target_volume+COALESCE((SELECT po.volume_ml FROM
physical_load_observation po WHERE po.request_id=m.request_id ORDER BY po.observed_at
DESC,po.observation_id DESC LIMIT 1),m.volume_ml)<=v.volume_capacity_ml) THEN
RAISE EXCEPTION 'transfer exceeds physical capacity' USING ERRCODE='23514'; END IF;
RETURN NEW; END $$
        """
    )
    op.execute(
        """
CREATE TRIGGER cargo_transfer_authority BEFORE INSERT ON cargo_transfer
FOR EACH ROW EXECUTE FUNCTION guard_cargo_transfer()
        """
    )
    op.execute(
        """
CREATE FUNCTION enforce_vehicle_safety_generation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
IF NEW.safety_version<OLD.safety_version THEN RAISE EXCEPTION 'safety generation cannot
decrease' USING ERRCODE='23514'; END IF;
IF ROW(NEW.blocked_reason,NEW.mass_capacity_g,NEW.volume_capacity_ml,NEW.profile_version)
IS DISTINCT FROM
ROW(OLD.blocked_reason,OLD.mass_capacity_g,OLD.volume_capacity_ml,OLD.profile_version)
THEN NEW.safety_version=GREATEST(NEW.safety_version,OLD.safety_version+1); END IF;
RETURN NEW; END $$
        """
    )
    op.execute(
        """
CREATE TRIGGER vehicle_safety_generation BEFORE UPDATE ON operating_vehicle FOR EACH ROW
EXECUTE FUNCTION enforce_vehicle_safety_generation()
        """
    )
    op.execute(
        """
CREATE FUNCTION guard_capacity_identity() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
IF TG_OP='INSERT' AND NOT EXISTS (SELECT 1 FROM operating_region r
 WHERE r.region_id=NEW.region_id AND r.version=NEW.region_version) THEN
 RAISE EXCEPTION 'capacity generation does not match region' USING ERRCODE='23514'; END
IF;
IF NEW.request_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM collection_request q WHERE q.request_id=NEW.request_id
 AND q.customer_id=NEW.customer_id AND q.serviceability_context_id=NEW.context_id
 AND ST_Equals(q.pickup_location::geometry,NEW.pickup_location::geometry)
 AND ((q.slot_start=NEW.slot_start AND q.slot_end=NEW.slot_end) OR EXISTS (
   SELECT 1 FROM appointment_consent a WHERE a.request_id=q.request_id
   AND a.reservation_id=NEW.reservation_id
   AND a.slot_start=NEW.slot_start AND a.slot_end=NEW.slot_end))) THEN
 RAISE EXCEPTION 'capacity binding does not match customer appointment' USING
ERRCODE='23514';
END IF;
RETURN NEW; END $$
        """
    )
    op.execute(
        """
CREATE CONSTRAINT TRIGGER capacity_identity AFTER INSERT OR UPDATE ON
regional_capacity_reservation
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION guard_capacity_identity()
        """
    )
    op.execute(
        """
CREATE FUNCTION guard_operating_region_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
IF (to_jsonb(NEW)-'version') IS DISTINCT FROM (to_jsonb(OLD)-'version')
 OR NEW.version < OLD.version THEN
 RAISE EXCEPTION 'region configuration is retained history' USING ERRCODE='23514'; END IF;
RETURN NEW; END $$
        """
    )
    op.execute(
        """
CREATE TRIGGER operating_region_history BEFORE UPDATE ON operating_region
FOR EACH ROW EXECUTE FUNCTION guard_operating_region_history()
        """
    )
    op.execute(
        """
CREATE FUNCTION guard_cargo_intent() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target UUID;
BEGIN
SELECT vehicle_id INTO target FROM operating_shift WHERE shift_id=NEW.to_shift_id;
IF TG_OP='INSERT' THEN
 PERFORM 1 FROM collection_request WHERE request_id=NEW.request_id FOR UPDATE;
 PERFORM 1 FROM operating_vehicle WHERE vehicle_id IN (NEW.from_vehicle_id,target)
 ORDER BY vehicle_id FOR UPDATE;
 IF NEW.from_vehicle_id=target OR EXISTS (SELECT 1 FROM cargo_transfer_intent i
 JOIN operating_shift s ON s.shift_id=i.to_shift_id WHERE i.resolved_at IS NULL
 AND (i.from_vehicle_id IN (NEW.from_vehicle_id,target) OR s.vehicle_id IN
 (NEW.from_vehicle_id,target))) OR EXISTS (SELECT 1 FROM route_task t JOIN vehicle_route r
 ON r.route_id=t.route_id WHERE t.state='STARTED' AND r.vehicle_id IN
 (NEW.from_vehicle_id,target)) THEN
 RAISE EXCEPTION 'transfer resources are already in use' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END IF;
IF TG_OP='DELETE' OR OLD.resolved_at IS NOT NULL OR
 (to_jsonb(NEW)-ARRAY['resolved_at','resolution','resolved_by','resolution_evidence_id'])
 IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['resolved_at','resolution','resolved_by','resolution_evidence_id'])
THEN
 RAISE EXCEPTION 'transfer authorization history is immutable' USING ERRCODE='23514'; END
IF;
IF NEW.resolution='TRANSFERRED' AND NOT EXISTS (SELECT 1 FROM cargo_transfer c
 WHERE c.intent_id=NEW.intent_id AND c.confirmed_by=NEW.resolved_by
 AND c.transferred_at=NEW.resolved_at) THEN
 RAISE EXCEPTION 'transfer resolution requires custody fact' USING ERRCODE='23514'; END
IF;
IF NEW.resolution='ABORTED' AND (EXISTS (SELECT 1 FROM cargo_transfer c WHERE
 c.intent_id=NEW.intent_id) OR NOT EXISTS (SELECT 1 FROM evidence_capture e
 WHERE e.evidence_capture_id=NEW.resolution_evidence_id
 AND e.captured_by_user_id=NEW.resolved_by AND e.captured_at>=NEW.created_at)) THEN
 RAISE EXCEPTION 'transfer abort requires attributable inspection' USING ERRCODE='23514';
END IF;
RETURN NEW; END $$
        """
    )
    op.execute(
        """
CREATE TRIGGER cargo_intent_resources BEFORE INSERT OR UPDATE OR DELETE ON
cargo_transfer_intent FOR EACH ROW EXECUTE FUNCTION guard_cargo_intent()
        """
    )


def downgrade() -> None:
    op.execute(
        """
DO $$ BEGIN IF EXISTS (SELECT 1 FROM routing_fleet_wakeup) OR EXISTS (SELECT 1 FROM
operating_region) OR EXISTS (SELECT 1 FROM operating_region_cell) OR EXISTS (SELECT 1 FROM
operating_region_receiving_point) OR EXISTS (SELECT 1 FROM operating_region_vehicle) OR
EXISTS (SELECT 1 FROM operating_break) OR EXISTS (SELECT 1 FROM operating_shift_extension)
OR EXISTS (SELECT 1 FROM shift_extension_consent) OR EXISTS (SELECT 1 FROM
capacity_admission_request) OR EXISTS (SELECT 1 FROM cargo_transfer_intent) OR EXISTS
(SELECT 1 FROM discharge_authorization) OR EXISTS (SELECT 1 FROM flexible_route_offer) OR
EXISTS (SELECT 1 FROM physical_load_observation) OR EXISTS (SELECT 1 FROM
regional_capacity_reservation) OR EXISTS (SELECT 1 FROM admission_flexible_offer) OR
EXISTS (SELECT 1 FROM appointment_consent) OR EXISTS (SELECT 1 FROM cargo_transfer) OR
EXISTS (SELECT 1 FROM cargo_transfer_receipt) OR EXISTS (SELECT 1 FROM customer_route_eta)
OR EXISTS (SELECT 1 FROM flexible_commitment) OR EXISTS (SELECT 1 FROM
route_acknowledgment) OR EXISTS (SELECT 1 FROM route_delivery) OR EXISTS (SELECT 1 FROM
route_resume_authorization) OR EXISTS (SELECT 1 FROM admission_flexible_commitment) OR
EXISTS (SELECT 1 FROM pickup_reattempt) OR EXISTS (SELECT 1 FROM reattempt_authorization)
THEN RAISE EXCEPTION '0027 downgrade requires preservation of operational history'; END
IF; END $$
        """
    )
    op.execute(
        """
CREATE OR REPLACE FUNCTION enforce_vehicle_route_binding() RETURNS trigger LANGUAGE
plpgsql AS $$
      BEGIN
        IF TG_OP = 'UPDATE' AND
          ROW(NEW.route_id, NEW.vehicle_id, NEW.shift_id, NEW.assignment_id) IS DISTINCT
FROM
          ROW(OLD.route_id, OLD.vehicle_id, OLD.shift_id, OLD.assignment_id) THEN
          RAISE EXCEPTION 'route binding is immutable' USING ERRCODE = '23514';
        END IF;
        IF NOT EXISTS (
          SELECT 1 FROM operating_shift s JOIN rider_assignment a ON a.rider_id =
s.rider_id
          WHERE s.shift_id = NEW.shift_id AND s.vehicle_id = NEW.vehicle_id
          AND s.resource_kind = 'COMMITTED' AND a.assignment_id = NEW.assignment_id
          AND a.source = 'COMMITTED_DISPATCH'
        ) THEN RAISE EXCEPTION 'invalid vehicle/shift/assignment binding' USING ERRCODE =
'23514';
        END IF;
        IF TG_OP = 'UPDATE' AND OLD.status = 'CLOSED' AND NEW IS DISTINCT FROM OLD THEN
          RAISE EXCEPTION 'closed route is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
      END $$;
        """
    )
    op.execute(
        """
DROP FUNCTION guard_cargo_transfer() CASCADE
        """
    )
    op.execute(
        """
DROP FUNCTION guard_capacity_reservation() CASCADE
        """
    )
    op.execute(
        """
DROP FUNCTION enforce_vehicle_safety_generation() CASCADE
        """
    )
    op.drop_table("reattempt_authorization")
    op.drop_table("pickup_reattempt")
    op.drop_table("admission_flexible_commitment")
    op.drop_table("route_resume_authorization")
    op.drop_table("route_delivery")
    op.drop_table("route_acknowledgment")
    op.drop_table("flexible_commitment")
    op.drop_table("customer_route_eta")
    op.drop_table("cargo_transfer_receipt")
    op.drop_table("cargo_transfer")
    op.drop_table("appointment_consent")
    op.drop_table("admission_flexible_offer")
    op.drop_table("regional_capacity_reservation")
    op.drop_table("physical_load_observation")
    op.drop_table("flexible_route_offer")
    op.drop_table("discharge_authorization")
    op.drop_table("cargo_transfer_intent")
    op.drop_table("capacity_admission_request")
    op.drop_table("shift_extension_consent")
    op.drop_table("operating_shift_extension")
    op.drop_table("operating_break")
    op.drop_table("operating_region_vehicle")
    op.drop_table("operating_region_receiving_point")
    op.drop_table("operating_region_cell")
    op.drop_table("operating_region")
    op.drop_table("routing_fleet_wakeup")
    op.execute(
        """
DROP FUNCTION guard_capacity_identity() CASCADE
        """
    )
    op.execute(
        """
DROP FUNCTION guard_operating_region_history() CASCADE
        """
    )
    op.drop_index("ix_outbox_routing_wakeup", table_name="outbox_event")
    op.drop_column("operating_vehicle", "safety_version")
    op.execute(
        """
DROP FUNCTION guard_cargo_intent() CASCADE
        """
    )
