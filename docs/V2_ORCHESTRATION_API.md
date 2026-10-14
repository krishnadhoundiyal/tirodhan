# Increment 7 frontend contract

All paths below require authenticated live roles. The new router is registered only when
orchestration, fleet and dispatch flags are enabled. This backend change does not edit the
frontend repository or enable production. UUIDs are references, not authorization tokens.

## Customer admission and appointment

1. Continue using the existing serviceability, date/slot and commercial eligibility APIs.
2. `POST /v2/customer/capacity-admissions` returns 202 with an admission UUID. Body:
   `client_command_id`, owned `context_id`, `slot_start`, `slot_end`, declared `mass_g`,
   `volume_ml`, `pickup_seconds`, `discharge_seconds`; optional `reattempt_request_id`.
   The backend applies configured conservative service/load allowances. Do not send coordinates.
3. `GET /v2/customer/capacity-admissions/{admission_id}` returns status, failure code,
   capacity class, usable reservation UUID, expiry and `book_by`/`bookable`. Flexible offers
   awaiting acceptance are UNCONFIRMED supply. SUCCEEDED is a capacity result, not payment,
   assignment, a commercial cutoff waiver or a promise that stale conditions remain valid.
4. Supply `capacity_reservation_id` to the existing `POST /v1/collection-requests` when the
   orchestration flag is enabled. The existing commercial checks and payment expiry still
   apply. An expired/used/mismatched certificate conflicts. Exact booking replay returns its
   original booking. New admission needs a new command UUID after a terminal expired result.

For a reattempt, use the original booking/context references. A new window is not effective
until `POST /v2/customer/operations` with `operation=APPOINTMENT_CONSENT`,
`client_command_id`, `request_id`, `reservation_id`. This records consent; it does not rewrite
the original quotation, payment, commercial slot or cancellation rights. Already owned or
started work cannot be rescheduled by this command. Customer charges/terminal retry rules
have not changed. Manager review can be required before another technical attempt.

`GET /v2/customer/collection-requests/{request_id}/routing` is ownership-scoped. States include
UNASSIGNED, ASSIGNED, RECOVERY_REQUIRED, APPOINTMENT_REQUIRES_REVIEW, IN_TRANSIT and terminal
booking states. It returns rider reference, effective service window and advisory ETA when
fresh. ESTIMATED/UPDATING describes ETA freshness. Do not display optimizer recommendations
as assignments or draw a live customer vehicle map. Rider name/contact fields are not invented;
their data/privacy contract remains separate.

## Rider synchronization and execution

Poll `GET /v2/rider/dispatch-sync`, then fetch the existing `GET /v2/rider/route` for tasks.
Persist the current revision UUID locally. A committed rider submits
`POST /v2/rider/operations` with `operation=ACKNOWLEDGE`, `client_command_id`, `revision_id`.
This confirms receipt. It is not a rejection/acceptance choice. Repeat the same UUID and body
after network ambiguity; conflicting reuse returns 409. A new revision needs its own receipt.
Departed/current destinations may continue under their protected task identity; superseded
uncommitted, cancelled and unauthorized work is fenced by the backend.

Flexible riders separately poll `/v2/rider/flexible-route-offers` and
`/v2/rider/flexible-admission-offers`. Submit FLEXIBLE_ACCEPT or ADMISSION_FLEXIBLE_ACCEPT
with `offer_id` and command UUID. Acceptance commits only that business offer, and may need
subsequent certification/activation. Two competing riders cannot both win. A route ACK
cannot substitute for flexible acceptance.

Use the existing `/v2/rider/route-tasks` mutation contract. NOT_COLLECTED additionally requires
`failed_reason` (CUSTOMER_UNAVAILABLE, UNCONTACTABLE, ACCESS_BLOCKED), `client_capture_id`
and `captured_at`. Capture appropriate PHOTO evidence; do not mark material COLLECTED to
advance the route. The response supplies the immutable attempt and evidence-capture reference.
Continue unaffected work. A later reattempt can have a different assignment around the same
stable pickup. Do not reuse an old task/revision as new authority.

BREAK records an interval on `shift_id`. At break start operations are blocked; at its end
they resume subject to current authority. Cargo stays onboard. EXTENSION_CONSENT is a rider
availability statement; a manager must independently authorize EXTENSION with the consent
reference and `safe_conditions_confirmed=true`. Existing duty expiry is not silently ignored.

## Recovery and media

Managers use `/v2/manager/operations` for REGION configuration, DISCHARGE_DESTINATION,
REATTEMPT_AUTHORIZE, LOAD_OBSERVE, RESUME_AUTHORIZE, EXTENSION and transfer commands.
These require live manager authority and command UUIDs. Region configuration includes
H3 cells, vehicle references, authorized points and the explicit default warehouse.

TRANSFER_PREPARE names request, donor vehicle and receiving shift. Both vehicles are held
while unresolved. Each rider submits TRANSFER_RECEIPT with scanned request identity and PHOTO
capture time. If the donor cannot act, a manager can submit TRANSFER_DONOR_WITNESS with
`physical_handoff_witnessed=true`; the witness is recorded as the actual capture actor.
The receiver still confirms personally. A receipt response's resource UUID is its evidence
capture UUID. TRANSFER_COMPLETE is the manager's final custody confirmation. It is not a
handover/delivery event. Expiry never releases the physical hold automatically.
Late receipt capture remains recordable on an unresolved intent; current manager confirmation
must still authorize resolution against live custody, receiving duty and physical capacity.

TRANSFER_ABORT needs scanned identity, `client_capture_id`, `captured_at` and explicit
`material_with_current_custodian=true`, with PHOTO inspection evidence. The manager's response
includes `evidence_capture_id`. Resolve physical uncertainty before resuming work.

Rider PHOTO upload uses the existing media APIs. Managers upload their own recovery captures
through `/v2/manager/media-assets`, its `/{id}/upload-authorization` and `/{id}/finalize`
endpoints. The existing private Blob ownership, type/size policy, short authorization and
finalization checks apply. A manager cannot upload another person's capture. Business capture,
custody/pickup/handover and media storage are separate: upload failure must not undo completed
work. Keep retryable capture IDs and photographs locally until upload succeeds.

Discharge keeps the existing scanned pickup/receiving identity, PHOTO, timestamp and geofence
requirements. Completing pickup does not make a loaded vehicle IDLE. Changing assignment alone
does not move cargo. Read fresh authoritative state after 409, reconcile the local command
result by replay, and preserve successful prior pickups during recovery.
