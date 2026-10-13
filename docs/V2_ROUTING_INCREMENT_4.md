# V2 increment 4: transactional route authority and vehicle-bound execution

Base: 3f8d3c64d8881269f2495954c9ef5cb8a30dec8a (current main, merged Increment 3).
Branch: codex/v2-transactional-dispatch. Implementation and validation date: 13 October 2026.

## Implemented operational boundary

This increment implements actual committed-resource assignments, ordered execution and
vehicle cargo attribution from the merged dual-engine evaluation contract. A reviewed
proposal remains advisory until its separate activation transaction succeeds. No offer
acceptance is required for an explicitly attested, currently active COMMITTED shift.
Fleet membership, AVAILABLE intent and IDLE are not substitutes for shift authority.
The manager attests the operating commitment through Increment 3's shift reference;
activation records the authenticated authorizing manager and the chosen pinned engine.

There is no new production deployment, scheduler registration or infrastructure.
TIRODHAN_ROUTING_DISPATCH_ENABLED defaults to false and controls the new API surface.
The independently configured shadow-review feature remains default false. Domain services
are transport-neutral; these settings gate runtime registration, not historical authority.
Do not activate production dispatch without review. After routes have been activated,
disabling their API is not a business rollback or a safe way to abandon their cargo/work.

## Persistent authority and compatibility

Migration 0025_vehicle_route_authority adds seven relational tables:

| Table | Authoritative meaning |
|---|---|
| route_activation | One successful activation per evaluated run, pinned engine and manager audit |
| vehicle_route | Stable vehicle/shift/assignment binding, current revision number and ACTIVE / RECOVERY_REQUIRED / CLOSED state |
| route_revision | Immutable ordered-route revision, source activation and predecessor |
| route_task | Stable pickup or whole-lot discharge task, pickup FK, destination, execution state and server timestamps |
| route_revision_stop | Immutable ordered task membership and independently assessed planned service times |
| route_recovery | Immutable manager recovery reason and route reference |
| route_discharge_proof | Immutable handover/task/photo-capture linkage and scanned receiving identifier snapshot; rejected attempts remain history |

Real FKs, unique run activation, one open route per vehicle/shift, unique route/version
and unique live stop per route protect identities. SQL triggers protect immutable
activation/revision/schedule/recovery/proof history, route bindings, shift identity and
legal task transitions. Vehicle/shift/performing assignment must agree. Completed and
cancelled tasks cannot be rewritten; started task timestamps cannot be moved. Timestamp
checks protect task lifecycle and route closure.

Existing RiderAssignment gains source COMMITTED_DISPATCH with required manager audit
and no CollectionGroup FK. V1 sources still require their historical group. PickupExecution
group membership becomes optional, allowing new V2 work without fabricated batches or
singleton groups. V1 rows keep their existing groups. Assignment responses permit a null
group; all V1 values remain unchanged. AssignmentItem gains CANCELLED release provenance.
Existing pickup/attempt, financial, evidence and handover identities remain stable.

No payment/refund algorithm, quote, provider integration or initiation trigger is changed.
Cancellation adds only routed-work detachment and the distinction between a routed booking
and an unrelated frozen V1 work unit. Downgrade is supported before any activation, with
populated V1 history retained. Once route_activation has history, downgrade refuses before
dropping data or altering legacy constraints. Recovery must drain/preserve those facts;
there is no lossy conversion to invented V1 groups.

## Activation transaction

The opt-in manager command selects one independently validated result from a SUCCEEDED
run, or the healthy candidate of an ENGINE_EVALUATION_FAILED run. Failure of one solver
does not discard the other engine's valid result. Readiness, deadline, staleness and other
failed runs remain ineligible. It checks engine identity/version, decodes the contract and calls
the independent validator again. It does not trust the stored assessment as permission.

One SERIALIZABLE PostgreSQL transaction claims manager/client command idempotency,
locks Payments in UUID order, acquires legacy work-unit advisory locks in key order,
locks booking rows, then locks sorted rider profiles, availability, live rider identity/
roles and vehicles. Request/profile/user/role locks use FOR NO KEY UPDATE, compatible
with operational-input recorder/rider FK checks. They still block eligibility updates.
Availability precedes user/role locking as in V1; profile locking precedes availability.
A real two-session test holds a position writer's vehicle lock while dispatch locks
authority and waits, then proves the recorder FK can finish without an authority/vehicle
cycle. No code obtains Payment after a resource/request lock.

The transaction reads the complete bounded source population again and compares it to
the evaluated fingerprint, including current routes, tasks, ownership and physical cargo.
SERIALIZABLE isolation supplies predicate/snapshot conflict detection; resource locks,
unique ownership and route constraints arbitrate assignment winners. Serialization or
deadlock aborts become RETRYABLE_TRANSACTION_CONFLICT, with no partial effects. The caller
may retry the same command after known rollback; SOURCE_CHANGED requires a fresh evaluation.
New arrivals serialized after activation remain eligible work for the next planning cycle.
There is no global city mutex or new distributed lock.

Used vehicles must be in currently active attested COMMITTED shifts, with live rider
authority, physical reservations and fresh positions. An unused future shift does not
block current capacity. Elapsed time is advanced before independent feasibility checking;
old predicted departure times cannot authorize a now-infeasible route. Existing active
V1 assignments and PRE_PLANNING batches must drain before import; a shadow recommendation
does not transfer those commitments implicitly. Ready, unassigned PLANNED pickups retain
their historical group but may join a route across groups/cells/slots.

New assignments, stable per-household execution, new revisions/task schedules, current
ownership, resource revisions, identifier-only VehicleRouteRevised outbox events and
command completion commit together. There are no solver, road, push or provider calls
inside the activation transaction. Exact replay returns original activation/revision
references even after later progress; it creates no additional assignment or event.

## Execution and continuous collection

One route assignment spans household groups and compatible slots. An already BUSY vehicle
can receive another feasible booking through a new revision without losing initial cargo
or creating a second concurrent assignment. Collected task/attempt history stays attached
to its original assignment. Revisions replace only remaining work; a STARTED next task
becomes the optimizer's protected prefix and cannot be reordered by either engine.

Rider task commands require current revision and next-task identity, live rider authority,
an open active shift and fresh position. They lock the same availability/vehicle resources.
Pickup commands acquire Payment/work-unit/request ancestors first, so cancellation and
physical pickup cannot win inconsistently. Starting pickup requires its appointment
service-start window and an executable booking. Successful pickup writes the conventional
PickupAttempt, COLLECTED execution, completed task and exact collecting vehicle observation
atomically. Capacity is checked again against unreleased vehicle cargo and integer physical
reservations. NOT_COLLECTED records history and retains the protected task until retry/recovery.

Collecting the last pickup **does not** complete a routed assignment or return its rider
to IDLE while discharge tasks remain. The vehicle remains available to V2 for feasible
additions while BUSY. Closure occurs when all route tasks are completed or explicitly
cancelled by recovery, with cargo discharge facts preserved. Planned end-node travel is
included in feasibility; no separate physical return-arrival fact is claimed by route
closure. Vehicle-position reporting and shift obligations remain distinct.

Whole-lot discharge requires the correct pickup UUID cargo label, selected registered
receiving identifier, an in-app PHOTO capture declaration, bounded capture timestamp,
authorized destination, opening interval/deadline and live PostGIS geofence validation.
Server registration/evaluation timestamps are authoritative; device capture time remains
a constrained client claim, not hardware attestation. Geofence-only legacy commands cannot
release V2 cargo. Wrong labels/identity/time reject the command. An outside-geofence attempt
persists REJECTED handover/evidence/proof history and leaves task/cargo unresolved; corrected
retry uses a new command/capture. Exact rejected replay returns the same rejected event.

Validated discharge persists handover items, photo-capture business facts/proof and task
completion together. Multiple rejected attempts can precede one validated discharge.
Media bytes/upload are independent and never roll back physical work. This does not
claim semantic image inspection, a finished label-printing/mobile flow, partial unloading
or physical transfer authority. Existing pickup evidence and Option B request completion
remain independently usable and are exercised through COMPLETED in native tests.

## Cancellation and recovery

Activation leaves advance ACCEPTED bookings ACCEPTED; it does not freeze commercial
rights because a route was optimized. The existing configured cutoff/refund policy still
applies. Actual pickup start transitions to PLANNED. Legacy freeze excludes every
V2-admitted booking, including released work, under the existing work-unit arbitration.
An unrelated V1 batch in the same cell/slot cannot freeze a routed booking's rights.
Customer read projections use the same distinction.

Cancellation detaches routed uncollected ownership/tasks in the same existing transaction
as cancellation/refund intent, invalidates the route for fresh pickup and appends recovery
notification metadata. Revoking a rider does not prevent the customer's cancellation.
Collected material cannot be cancelled through this hook.

Manager recovery checks the expected revision, releases only uncollected household work
with historical provenance, cancels its remaining tasks and records a recovery fact/event.
Successful pickups, performing assignment, vehicle cargo and pending discharge stay intact.
Revoked/suspended riders may be recovered by the live manager; this does not confer new
rider execution authority. Recovery can complete retained discharge on the same vehicle,
or a new independently feasible activation can explicitly resume that resource.

Released work cannot re-enter V1 dispatch or reclaim the same old assignment item. While
that route still holds cargo, its released pickups are excluded from its own eligibility;
other committed resources may take them. Once it drains/closes, a new route/assignment can
take fresh work without rewriting released history. No implicit physical custody transfer
or fabricated replacement rider is implemented.

## API, delivery and retirement

Default-disabled endpoints:

- POST /v2/manager/routes/activate (activation plus immutable revision references)
- GET /v2/manager/routes/{route_id} (current progress, internal IDs only)
- POST /v2/manager/routes/recover
- GET /v2/rider/route (only the authenticated rider's open route)
- POST /v2/rider/route-tasks (START, PICKUP, DISCHARGE)

Existing authorized rider assignment reads supply immutable household address/location
for current unreleased ownership. New route views expose no household coordinates, raw
matrix/problem or addresses. No sensitive payload enters outbox/idempotency/logs.
Every command has actor-scoped UUID/fingerprint replay and one transactional result.

V2-admitted work cannot return to legacy group dispatch, freeze or pickup/handover mutation
paths. Historical legacy command replay remains intact. V1 queues/drain readers are kept
only for unadmitted historical work until the approved cutover. There is no V1 fallback
for a V2 routing failure. Accepted work remains visible; failure cannot invent capacity
or drop fulfilment commitments.

Outbox events are durable but the existing publisher/notification consumer whitelist is
not extended to deliver these new types yet. They remain PENDING; no new queue is
provisioned or unrecognized event marked published. Rider polling works when enabled.
Automatic rolling orchestration, delivery and global V1 admission cutover remain separate
reviewable integration work, not a permanent parallel architecture.

## Validation and next increment

Completed validation of the final implementation:

| Check | Actual result |
|---|---|
| Full repository suite, Python 3.12.15 API dependencies, disposable PostgreSQL | **1,254 passed, 23 skipped, 4 warnings**, 326.68 seconds |
| Native routing and both operational integration suites, Python 3.13.5 | **110 passed**, 95.52 seconds |
| Final added failure/recorder-lock regressions under Python 3.12 | **2 passed**, 7.11 seconds |
| Ruff check / formatting / mypy | Passed; mypy checked 172 source files |
| Migration upgrade/downgrade, populated V1 and financial history, activation-loss refusal | Passed |
| Strict Alembic metadata comparison | No new drift; only the two existing collection-request index differences remain |
| Default API dependency integrity | pip check passed; OR-Tools remains absent from default installation |

The 23 standard-suite skips are opt-in native cases (18 existing pure/native routing,
3 Increment 3 operational and 2 new authoritative scenarios); the separate native run
executes all of them. The four default-runtime warnings are existing Firebase multicast
tokens deprecations. The database is PostgreSQL 17.5 / PostGIS 3.5.2, including extension-owned
geocoder/topology objects. Existing catalog-ownership filtering remains strict, including
the explicit checks for ix_collection_request_pickup_location_gist and
ix_collection_request_planning_batch_id. Financial migration history, initiation-term
triggers and two-session payment locking regressions execute in the complete suite.

An additional earlier full-suite run in the Python 3.13 native dependency image produced
1,251 passes, 23 skips and one failure in the unchanged TwoFactor sensitive-URL traceback
test, plus five warnings (four Firebase deprecations and one ByteStream coroutine warning).
The same failing test was reproduced against the exact main base commit in that image.
A diagnostic probe verified that only the literal OTP in the test's multiline source
excerpt appears: API key, session reference and phone are absent. Python 3.12 passes that
test and the complete current suite. No identity/provider test or sanitization logic was
changed to hide this pre-existing runtime-specific assertion failure.

Native cases use pinned OSRM
6.0.0, VROOM 1.15.0 and OR-Tools 9.14.6206 against the checksum-verified public Delhi graph
and synthetic operating facts. They establish actual dual-engine/domain integration,
not production road-profile accuracy, optimality or scaling. The runner used the cached
dependency image (unchanged manifest) and mounted the exact current source read-only.
Each native invocation created/removed its own PostgreSQL/OSRM/graph resources.

| Synthetic public-road scenario | VROOM | OR-Tools |
|---|---|---|
| Initial two-booking route | 348 travel seconds / 2,201 m | 348 travel seconds / 2,201 m |
| Remaining route after pickup, refreshed position and an additional booking | 147 travel seconds / 831 m | 147 travel seconds / 831 m |

These are separate snapshot projections, not additive journey totals. Both engines
activated their proposal, preserved collected cargo/assignment while revising, performed
all remaining tasks and reached existing evidence-based request COMPLETED state. Tests
also cover competing engine activations, V1 assignment races, cancellation races/replay
with compensation, source/authority/payment/time drift, protected prefixes, partial engine
failure, rollback before commit, geofence rejection/retry, cargo-preserving recovery,
legacy-path exclusion, immutable bindings/history and unused future-shift compatibility.
No production infrastructure or recurring-cost commitment was made.

The recommended next increment is single-owner rolling admission/orchestration with
proactive supplementary flexible offers/acceptance, durable route notifications and
safe V1 new-work retirement. It must use these authority/execution/recovery transactions,
retain historical replay/readers, and visibly escalate insufficient capacity or failed
optimization instead of treating them as customer failure.

Before activation: review real operating-attestation/consent capture, measured physical
reservations, position/profile calibration, receiving identifiers/windows and the mobile
label/photo flow. Explicit manifest correction, manager custody transfer/late discharge
exception, protected-prefix operational policy and retention/scale measurements remain
gaps. Position input currently uses manager resource commands; rider/device ingestion
and its operational cadence still need integration. Exact source comparison can invalidate
an entire bounded proposal when frequently updated telemetry changes during solving.
Rolling orchestration must measure this contention and choose coherent scopes/re-evaluation
without hiding drift or holding locks during external work.
Production OSRM/solver hosting is still unselected; no recurring-cost commitment
is made. Matrix memory remains quadratic and bounded SERIALIZABLE scopes can conflict
under contention; the existing 128-request/32-vehicle ceilings are technical limits,
not production service promises.
