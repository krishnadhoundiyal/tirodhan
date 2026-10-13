# V2 increment 3: durable operational inputs and recoverable shadow execution

Base: `9048d809874b5da8615643dd9466e7ea578d39a4` (current main, merged increments 1/2).
Branch: `codex/v2-operational-integration`.

## Implemented boundary

This increment joins the routing engines to real PostgreSQL booking, assignment, pickup
and handover facts. It implements relational operating inputs, a consistent snapshot
builder, manager review APIs, durable evaluation identities and a finite worker. It is
the resource/snapshot prerequisite to replacing V1, not the final operational cutover.
No proposal creates a PickupExecution, assignment, offer, custody event, payment/refund
change or outbox event. Authoritative dispatch remains absent, and the review API/worker
requires explicit `TIRODHAN_ROUTING_SHADOW_REVIEW_ENABLED=true` (default false).
No production configuration, scheduler registration or infrastructure is changed.

V1 cell/slot grouping and fleet-first voluntary dispatch are **superseded target designs**.
Retained V1 execution is a transition/drain mechanism, not a permanent parallel product.
Implementing dispatch before establishing physical inputs and execution/route ownership
would infer capacity and vehicle custody that the repository did not previously record.
This cohesive increment establishes and tests those inputs; committing operational route
revisions and integrating execution is the recommended next increment.

## Relational operational facts

Migration `0024_routing_operational_inputs` adds seven tables, with real foreign keys:

| Table | Meaning and protection |
|---|---|
| operating_vehicle | Explicit integer mass capacity (g), volume capacity (ml), road-profile version, end receiving point, recorder and positive revision |
| operating_shift | Vehicle/rider, COMMITTED or FLEXIBLE classification, bounded UTC period, manager attestation/reference, explicit closure; partial unique open shift per rider and per vehicle |
| vehicle_position | Timestamped PostGIS geography, monotonically increasing per-vehicle revision and recording identity; unique vehicle/revision |
| routing_manifest | One explicitly declared whole-household load reservation, pickup/unload service seconds, hard discharge deadline and selected authorized receiving point per request |
| routing_receiving_window | Explicit dated UTC opening intervals for an existing receiving point |
| cargo_vehicle_observation | Manager's explicit legacy vehicle observation tied to the successful historical PickupAttempt and request; one observation per request, not rider/custody transfer authority |
| routing_shadow_run | Unique manager/client command, fingerprint, configuration, attempt/lease, immutable versioned problem and independently checked results, controlled failure and terminal timestamps |

Upgrade is additive: existing booking, financial, assignment, pickup and handover tables
and triggers are unchanged. Downgrade drops only these seven new tables and is therefore
lossy for operating inputs and shadow results. Export those facts before any real schema
rollback. Generic resource-command idempotency records are retained; their stored result
IDs must not be treated as restored resources after dropping/recreating the schema. Use
review-feature disablement for ordinary runtime rollback; recovery of populated new inputs
requires a coordinated restore rather than rerunning old resource commands.

The shift's `commitment_id` is an externally supplied attestation reference, not a foreign
key to an invented internal agreement table. Recording a shift does not authorize route
assignment in this increment. Fleet membership, a rider's availability intent and IDLE do
not establish commitment. Production agreement/consent capture and binding to dispatch
must be completed before activation.

Manifest loads are explicit operational reservations, not quantities inferred from money,
catalogue names, optional declared item weights, vehicle class labels or group count.
One indivisible household lot has quantity 1 and two physical dimensions. The manager
selects one receiving point; there is no implicit nearest-facility authorization. Capacity
and reservation accuracy still need operational measurement/calibration. Resource creation,
manifest declaration, receiving windows and cargo observations have no edit command in
this increment. Correction/version-history commands and vehicle/custody transfer are not
implemented; do not use ad hoc SQL edits as operational commands.

Position reports lock the vehicle, reject future/non-increasing observations, increment
its revision and append history. Shift open/close use the same vehicle lock. Database
partial uniqueness arbitrates different vehicles concurrently enrolling the same rider.
Closure checks active assignments and unreleased unhanded work; it does not erase cargo.
These input checks are **not yet a dispatch concurrency fence** against V1 execution:
V1 assignment/pickup transactions do not acquire the new vehicle lock. Safe authoritative
adoption needs the coordinated locking/execution boundary in the next increment.

## Operational projection

One REPEATABLE READ transaction captures resources, live user/rider/role state, positions,
bookings, assignment ownership, successful pickup attribution, handover items, manifests,
receiving-point status/version/location and opening windows. It releases the transaction
before OSRM or either solver executes. A real two-session test changes several sources
between reads and proves that the captured values remain one MVCC snapshot.

New candidates are ACCEPTED/PRE_PLANNING/PLANNED, unhanded collections whose appointment
overlaps the explicit rolling horizon. No H3, slot, group, fleet membership or group-size
predicate limits route construction. Accepted advance bookings without PickupExecution
are included without freezing them. Outstanding work owned by a scoped rider is included
even outside the horizon; expired commitments produce a visible failure. An in-scope
assignment owned by an unscoped rider fails readiness rather than reassigning it.

Only explicitly recorded **committed** capacity participates in this first evaluation
stage. BUSY/RESERVED/IDLE and OFFLINE intent do not discard a committed shift's existing
obligations. Suspended/disabled users, missing live rider roles and missing/stale positions
fail readiness. FLEXIBLE shifts are persisted but do not compete in the committed stage;
the supplementary offer/acceptance stage remains a dispatch integration dependency.

Initial cargo comes from successful PickupAttempt performing assignments, excluding only
whole-household items whose HandoverEventItem and parent HandoverEvent are both VALIDATED.
Onboard cargo is included by scoped vehicle
observation as well as custodian, so a changed shift cannot hide a prior rider's cargo.
Booking/financial status cannot erase recorded physical debt. Assignment completion, IDLE, media upload
and request completion are not cargo-release signals. Legacy cargo requires an explicit
vehicle observation matching that successful attempt and the scoped custodian's vehicle.
The builder never guesses that a rider's later shift vehicle contains earlier pickups.
Existing uncollected assignment ownership becomes hard vehicle binding. V1 has no durable
stop sequence, so no protected order is fabricated; V2 route revisions must introduce
the protected execution prefix before authoritative suffix replacement.

Both pickup and discharge are required for new lots. Onboard lots have only discharge.
Shift/end-node travel, cargo conservation, mass/volume capacity, appointments, service
time, authorized discharge, opening intervals and deadlines pass through the unchanged
common contract and independent validator. Starts round upward; ends/deadlines round
downward to contract seconds. Overlapping declared opening intervals are unioned.
No partial physical handover is invented: the source execution model records whole lots.
Quantitative partial discharge/transfer plus required V2 kiosk identification/evidence
are dependencies before such operations can become authoritative.

Scope limits are explicit and reject overflow; no LIMIT silently trims the candidate
population. Technical ceilings are 128 requests, 32 vehicles, 512 nodes and 8 profiles,
with a maximum one-day horizon and 30-second per-engine search budget. They are local
working-set bounds. Scope epoch/horizon use whole UTC seconds; source-window starts/ends
are projected conservatively. Opening-window materialization is also bounded (16 times
the configured node limit); overflow fails instead of trimming intervals. These are
not booking admission rules or production sizing claims. Missing
manifest/cargo/position/opening facts produce durable readiness codes; a captured failed
scope records internal request/vehicle IDs. Accepted work remains in its existing state.

## Run identity, failure and recovery

`POST /v2/manager/routing/resources` uses `idempotency_record` scoped to the live manager,
plus the client command UUID and complete fingerprint. The mutation and result complete
in one transaction. Exact concurrent replay returns the same resource. Changed payload
conflicts; independent conflicting commands are arbitrated by domain keys/constraints.

`POST /v2/manager/routing/shadow-runs/{client_command_id}` reserves one durable PENDING
run under unique manager/command identity. Configuration changes with the same key
conflict. The worker claims the run row in a short transaction, increments its attempt
and commits a finite lease with a fresh token. Concurrent workers cannot claim a live
lease. Evaluation uses both approved engines on the same persisted RoutingProblem.
The worker independently validates each proposal again and checks its pinned engine
identity. Failure of one engine does not prevent evaluation/reporting of the other.

The process command is explicit and finite:

```text
python -m tirodhan.workers.routing_shadow <run_uuid> <profiles_json_file>
```

The profile file is an array containing endpoint, graph_version, profile_version,
max_snap_distance_m and timeout_seconds. It contains no secret or customer coordinates.
It must identify the explicitly selected road graph/profile. Default API dependencies
still exclude OR-Tools; run native execution in an isolated fully resolved routing image.
The worker uses the existing database engine/settings (including workload identity),
bounded OSRM clients without transport retries, and the pinned Increment 2 adapters.

Crash before persisting the problem may reacquire its read-only matrix. Crash/cancellation
after persisting it resumes the **same** problem after lease expiry, without reacquisition,
if sources still match. Old tokens cannot save or finalize after another claim. Exact
terminal replay returns the persisted result without calls; corrected input/failure retry
requires a new command, preserving the prior result. A finite overall deadline reserves
five seconds of the lease for finalization. A lost/expired lease leaves recoverable state
instead of permitting an old worker to overwrite its successor.

Source fingerprints include scoped population and actual physical/authorization/ownership
facts, not just rider state. Known changes/new arrivals, cancellation, reassignment,
position, facility or role changes mark a result STALE. Freshness/horizon are checked
again. **SUCCEEDED means feasible for the immutable snapshot**, not presently assignable.
The final fingerprint comparison is an observation, not an atomic dispatch fence; facts
may change immediately afterwards. Future dispatch must lock/fence current resources,
route revisions and ownership, revalidate live authority/custody and arbitrate cancellation
with the established Payment-first rules before creating any assignment.

No broker/outbox is needed for this manually invoked read-only external evaluation.
OSRM acquisition and solving may repeat after ambiguity; neither changes external
business state. No result is a financial, physical or dispatch side effect.

## Transition and next increment

1. Deploy schema only after review; populate/verify operating facts and benchmark shadow
   runs over real eligible bookings. No change to V1 runtime or customer rights yet.
2. Implement transactional V2 route revisions, committed-shift assignment and execution
   binding to stable PickupExecution. Coordinate resource/authorization/ownership locks
   with pickup, handover and recovery. Bind physical collection to its vehicle and reserve
   protected execution prefixes. Supplement with idempotent voluntary flexible offers.
3. Separate cancellation policy/cutoff configuration from operational projection cadence;
   advance proposals remain provisional. Introduce one admission/cutover owner so each new
   eligible booking enters V2 exactly once, while existing V1 batches/offers drain. Do not
   make two planners compete over the same request or retain V1 as a permanent fallback.
4. Preserve existing PickupExecution, assignment/pickup-attempt/evidence/handover/financial
   history. Carry in-flight cargo explicitly; never return its capacity to zero on IDLE.
   Recovery retains collected work and immutable incumbent ownership until valid transfer.
5. After operational end-to-end review/activation approval, retire new V1 batch discovery,
   greedy compaction, group-based fleet-first offers and group-required assignment paths.
   Keep historical records and any drain/replay reader needed for already accepted work.

Production activation, operating agreement capture, physical reservation policy/measurement,
profile calibration, receiving-point authorizations/opening data, position freshness,
cancellation cutoff values, protected-prefix policy and kiosk/partial-discharge requirements
need explicit operational/product decisions. This implementation supplies no implicit
business thresholds and does not declare those prerequisites solved.

## Infrastructure and scaling

No paid matrix provider, new database, cache, cloud service, always-on routing instance,
Terraform or production container change is introduced. Local bounded PostgreSQL/PostGIS,
OSRM graph and solver containers are disposable. The reproducible operational runner is
`tests/routing/run-operational.ps1`; the earlier solver-only runner retains its own image
target and behavior.

Production OSRM hosting remains undecided: finite graph-loaded Job/co-location versus a
separately scaled ACA component with cold-start costs versus a retained replica requiring
recurring-cost approval. No cost estimate is invented without region, graph, memory and
invocation measurements. Solvers fit finite independently scaled execution; optional
native dependencies remain outside the default API installation.

Matrices still cost O(profiles × nodes²), and solver search remains combinatorial. The
latest-position aggregate scans position history; source queries/ownership joins and dense
JSONB snapshots need sizing, indexes/current-position projection and a retention policy
before sustained production use. JSONB stores versioned contract/result documents only;
operating/custody facts remain relational. Exact coordinates stay in restricted PostGIS
tables and transient matrix calls, never run results, reliability metadata, logs or broker
messages. Distances/schedules and internal route IDs are also restricted manager data.

## Validation

Completed on 2026-10-12 against this branch's implementation:

| Check | Actual result |
|---|---|
| Complete repository suite, default dependencies, Python 3.12.15, disposable PostgreSQL | **1,230 passed, 21 skipped, 4 warnings**, 443.26 seconds |
| Pinned native routing + operational integration suite, Python 3.13.5, separate disposable PostgreSQL | **84 passed**, 101.40 seconds; all 34 operational cases included |
| Ruff check and formatting check | Passed |
| mypy `src` | Passed, 167 source files |
| `git diff --check` | Passed |
| Fully resolved optional native image `pip check` | No broken requirements |

The 21 full-suite skips are explicitly opt-in native tests (18 existing routing cases,
3 new operational scenarios); all execute in the separate native run. The four warnings
are existing Firebase MulticastMessage.tokens deprecations in dispatch push tests.
PostgreSQL is **17.5**, PostGIS **3.5.2**, including extension-owned geocoder/topology
objects. The full suite includes the existing populated financial-history migration,
initiation triggers and two-session payment-term regressions. The new migration test
cycles 0024 down/up around a populated booking and compares Alembic metadata using the
existing catalog-ownership filter: only the two previously documented application index
differences remain (`ix_collection_request_pickup_location_gist` and
`ix_collection_request_planning_batch_id`); there is no new schema drift.

The native runner uses OSRM **6.0.0**, VROOM **1.15.0** and OR-Tools **9.14.6206**.
OSRM/VROOM are inherited digest pins from Increment 2; the road fixture is verified by
SHA-256 before graph preparation. The dependency image was built using the new
operational-tests target, and the final run used `run-operational.ps1 -SkipBuild` with
the exact current source mounted read-only. Each invocation creates and removes its own
PostgreSQL, OSRM and graph volume, without host ports or production access.

| Public-road scenario, synthetic operational facts | VROOM | OR-Tools |
|---|---|---|
| New cross-cell/cross-slot bookings | 348 travel seconds, 2,201 m | 348 travel seconds, 2,201 m |
| BUSY vehicle with existing ownership | 348 travel seconds, 2,201 m | 348 travel seconds, 2,201 m |
| Initial cargo requiring discharge before another pickup | 287 travel seconds, 1,898 m | 287 travel seconds, 1,898 m |

Both proposals in each scenario pass the independent feasibility validator. Travel totals
exclude waiting and service time; validator schedules include both. These small Delhi
fixtures establish integration and feasibility, not production scale or global optimality.

Concurrency/recovery coverage includes real MVCC consistency across changing sources,
concurrent command replay and shift uniqueness, waiting shift closures, snapshot drift,
expired leases and obsolete-worker fencing, cancellation after persisted problem capture,
rollback before command commit, matrix failure, invalid engine output, physical cargo
surviving IDLE/booking-state changes, and incomplete handover evidence. The first full
run had 1,229 passes and one failure in the new closure test's lock observer, which assumed
both writers would be directly blocked by one backend. Observing PostgreSQL tuple and
transaction locks corrected that test barrier; focused and complete reruns above passed.
No unresolved test failures remain.
