# Geographical rolling QA execution contract

Base: `eefabf5c2174533653f71bdf7e961a039c46b261` (merged PR64). Synthetic identities,
appointments and declared capacities; public OSM road coordinates only. No production enablement.

## Actual execution chain

1. `collection_requests/service.py:create_collection_request` creates the logical booking and
   Payment; binding a projected slot hold uses `routing_operations/admission.py:bind_reservation`.
   `payments/service.py:process_authenticated_payment_event` accepts authenticated test capture
   and transitions the booking to ACCEPTED. Neither a hold nor a solver proposal is an assignment.
   Operational eligibility also needs a `RoutingManifest`, region-cell ownership and an authorized
   receiving point with dated opening windows (`routing_operations/snapshot.py:_read_sources`).
2. `operational_commands.py:ConfigureRegion` creates relational region/cell/vehicle/facility
   membership. `commands.py` creates vehicles, manager-attested shifts and position observations;
   `operational_commands.py:ConfirmShift` records rider confirmation **before** shift start.
   Regional snapshots require confirmed, active shifts. One vehicle has one capacity-owning region.
3. `orchestration.py:wake/prepare_due` consumes financial outbox facts through the inbox and
   schedules region-specific wake generations. `regional_planner.py:regional_wake` coalesces
   signals; it does not represent completion of individual bookings. There is **no booking-count
   accumulation trigger**. A labelled QA driver accumulates 20/40/60 new paid bookings and invokes
   the real `regional_planner.py:run_region`; polling/time wakes remain production mechanisms.
4. `run_region` claims `FleetControl(scope=REGION:<uuid>)` with a token/expiry; reconciles
   cancellation/recovery; captures a consistent regional snapshot; retains mandatory ownership,
   protected work and cargo; selects a bounded optional candidate set; builds an OSRM road problem.
   `max_admitted`, `max_discovery`, neighbourhood and node budgets are computation limits, not
   proofs of fleet capacity. Exclusions must have durable per-booking outcomes and retries.
5. `parallel.py:local_workers` uses bounded spawned processes and `solve_task`. Each engine uses
   its real adapter (`planning/routing/adapters.py`), followed by independent `validate_proposal`.
   Workers cannot create assignment/custody/financial authority. Complete-route alternatives feed
   `parallel.py:consolidate`: actual OR-Tools **CP-SAT**, distinct from OR-Tools routing.
6. CP-SAT requires one alternative per vehicle, at most one per rider, exact mandatory-lot
   coverage, optional-lot uniqueness and configured unloading-concurrency limits. Lexicographic
   stages prioritize escalation/urgency, coverage, committed over supplementary capacity and cost.
   Bounds/statuses establish optimality only for the supplied pool and completed stages.
7. `regional_planner.py:refresh_selection` rebases/revalidates/reselects before publication.
   `authority.py:_activate` uses SERIALIZABLE, Payment-first booking and common resource locks,
   current source fingerprints, composite selection identity, confirmed shifts and owner fences.
   Immutable `RouteRevision`/stops, stable `PickupExecution`, conventional `RiderAssignment`/items,
   cycle outcomes and captured-generation acknowledgement commit together. Other wake generations
   survive. No direct final-assignment fixture insertion is permitted in this QA suite.
8. `execution.py:execute_task` enforces current revision, ordered tasks and DEPART/START protected
   work. Successful pickup writes the conventional attempt and cargo observation atomically.
   Discharge requires the existing scanned identity, evidence and receiving-point workflow.
   `recovery.py` releases only uncollected work; cancellation uses the existing Payment-first
   transaction. Completed work/cargo never disappear because a suffix is replanned or a shift ends.

## Clock and horizon semantics

`rolling.py:FleetPolicy.horizon_seconds` sets snapshot `horizon_end = epoch + horizon` (including
travel/discharge). Optional `pickup_horizon_seconds` independently restricts pickups and must not
exceed the routing horizon. `planning_lead_seconds` is independent of cancellation cutoff;
`dispatch_readiness_seconds` is a separate departure/readiness guard. Appointment service-start
windows, shift ends and dated facility windows remain hard constraints. QA uses a documented
controlled operational clock; real native elapsed times are measured separately.

## Observation and strict acceptance

Observers must forward real production calls unchanged. Record native worker tasks/results and
actual coordinator inputs/results, including refresh calls; NOT_INVOKED is not success. Before
arrival and after publication, query authoritative relational state and retain cumulative booking,
ownership, protected-task and cargo history. Validate persisted composite proposals independently.
The acceptance test fails if dependency prerequisites or required successful cycles are absent.
Partial runs and provider/solver failures remain evidence, never invented successful assignments.

Adjacent overlap means **eligible for an atomic transfer**, never simultaneous regional capacity.
`regional_commands.py:transfer_capacity` refuses promises, assignments and cargo and version-fences
the single owner. Dedicated-fleet and overlap/recovery scenarios must be reported separately.

Relevant authority: ADR-004/006, PROJECT_CONTEXT, ARCHITECTURE, DOMAIN_MODEL, SCHEMA_DESIGN,
ER_DIAGRAM, IDEMPOTENCY and DATA_PROTECTION. No business semantics, migrations or production flags
are changed to obtain QA success.
