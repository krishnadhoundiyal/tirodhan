# Regional commercial capacity and rider recovery

Base: `d9b676d74fd84394789b1909c2afa0335f5b69a9`.
Implementation branch: `codex/commercial-admission-scalability`.
This increment does not enable production routing or provision infrastructure.

## Decisions implemented

An operating region remains a collection of canonical resolution-7 H3 cells, not a new
geocoding scheme. Existing region configuration can now declare an empty fleet, allowing
capacity to be transferred in subsequently. Regions may approximate a desired service area;
20 km² is not a new hardcoded size or a demonstrated optimal boundary. There is no automatic
Delhi redistricting or permanent prohibition on adjacent-region service.

An available rider/vehicle may move to an adjacent region through `CAPACITY_REGION_TRANSFER`.
The vehicle has one versioned `operating_region_vehicle` row. Existing unique open-shift
constraints ensure the rider does not supply independently open shifts on multiple vehicles.
Historical memberships/certificates do not constitute a second live capacity owner.

The transfer is manager-authorized and SERIALIZABLE. It checks the expected source/target
region versions and membership version, active duty, live rider authorization, AVAILABLE/IDLE
state, H3 adjacency, assignments, open routes, transfers, actual cargo, live holds and accepted
commercial allocations. It locks resources before sorted regional/source publication fences.
Membership, immutable transfer audit, source invalidations and both regional wakes commit together.
Failure rolls back the command; exact replay returns the original transfer identifier.

This deliberately transfers **idle capacity**, not outstanding obligations between planning
owners. A resource with commitments must drain/recover them first. Operational cargo handoff
is a different existing domain operation and must not be simulated by moving membership.
Shift close also checks certified commitments under the same source-head fence. A database
trigger protects against closing a shift while those promises remain; closing/reopening duty
cannot bypass the transfer guard. Collected cargo continues to use the existing custody and
shift-close guards after natural expiry.

## Rider replacement

Replacement is explicit per stable household cargo lot/CollectionRequest. Several lots use
several attributable requests/transfers; this is not a whole-vehicle reset or a partial-lot model.

1. The current assigned rider or effective custodian submits `REPLACEMENT_REQUEST`.
2. The finite regional worker computes a recommendation. A manager may request a new search
   with `REPLACEMENT_RECOMMEND`. The manager reads its version, shift, source version and timestamp.
3. The manager sends `REPLACEMENT_DECIDE`, APPROVE or REJECT, against the expected version.
4. Before pickup, approval releases only that outstanding assignment item through existing
   recovery mechanics. It preserves the accepted booking and stable PickupExecution. STARTED
   household service must finish or use the explicit incident/recovery workflow first.
5. Approved uncollected work is constrained to the recommended shift in ordinary regional
   problem construction. Normal independently validated composite activation creates its new
   assignment/tasks/immutable route revision and marks replacement complete transactionally.
6. After pickup, approval creates the existing CargoTransferIntent. It does not move custody.
   Existing donor/receiver PHOTO evidence receipts, or the existing attributable donor-witness
   path, and manager completion are required. Receipt capture remains independent of upload.
7. Validated completion appends the existing immutable CargoTransfer, cancels obsolete donor
   discharge tasks, updates receiver recovery state and wakes both vehicle owners in the same
   transaction. The next regional cycle builds/activates a route containing the receiver's
   mandatory discharge. Planning/provider failure cannot undo a physical transfer or omit cargo.

Original PickupAttempt attribution is never rewritten. Effective custody follows the existing
transfer sequence until actual validated receiving-point handover. Expired duty does not
erase cargo or make a rider commercially available. An independently healthy regional resource
can be certified while an expired resource's owned obligations remain explicitly quarantined.
Unowned malformed commitments still block certification; there is no blanket ignore-errors filter.

Recommendations preserve the target's complete known local obligations and pass the independent
validator. They use fixed-order pair insertion, discharge insertion for transferred cargo and
the existing native adapters for bounded repair. Committed resources precede consented flexible
resources; feasible alternatives are ranked by incremental travel against an available route
proof, or total travel when no comparable proof exists. This is best-found local advice, not a
proof of global optimality, a reservation, rendezvous planning or assignment authority.

Failure retains REQUESTED with bounded retry backoff; it does not claim physical infeasibility.
Manager approval rechecks versions, current ownership/custody and active target duty. A changed
source rejects stale advice. For approved uncollected work, a manager can authorize a fresh
target search if the target becomes unavailable. The old target constraint remains until a
source-fenced feasible replacement is published; normal activation still verifies the result.
A pending physical transfer must first be resolved through its
evidenced abort/complete workflow. Decision rows and capacity-transfer rows are append-only.

Existing transfers require a different receiving vehicle and an active receiving shift.
Same-vehicle successor-shift handover is not introduced: it requires a separately specified
transition through the existing immutable shift binding and one-open-vehicle-shift constraint.
Managers can use the implemented different-vehicle custody recovery without falsifying duty.

## Advisory slot projections

`regional_slot_projection` is an ordinary relational read model in the existing database.
It contains region/date/slot, authoritative source version, computation/expiry timestamps,
status and aggregate workload counters. It contains no addresses, coordinates or phone numbers.

For each resource, union calendar exclusions before constructing free intervals. The sweep
integrates simultaneous eligible committed resources:

\[
A_r(t)=\sum_{v\in r}\mathbf1[t\text{ is in a declared free interval of }v],\qquad
C_{r,s}=\int_{s.start}^{s.end}A_r(t)\,dt.
\]

The implementation aggregates accepted uncollected bookings, including operationally unplanned
ones, and unexpired holds/bound pending-payment holds. A bound paid booking is counted as a
booking rather than both booking and hold. Existing minimum service times are used. Breaks,
shift extensions, current route spans and effective unhanded cargo contribute workload signals.
Expired loaded resources are excluded from supply, not cleared of custody.

Status is FULL when there is no overlapping declared supply, including the inclusive service-start
boundary; LIMITED when workload pressure reaches that supply, otherwise AVAILABLE. Route spans
can include waiting, so this is explicitly a pressure signal. It is not a route feasibility proof.
Mass/volume, road direction, service-start promises, authorized discharge, all accepted obligations
and the existing conservative assumptions remain mandatory in actual admission certification.
Region-level availability cannot promise suitability for every customer address or load.
There is no additional percentage reserve and no multiplication of existing safety percentages.

Missing/stale projections are not capacity authorization. Reads compare the projection version
to the current source head and its expiry, returning UNKNOWN when stale. Customer selection
still uses the existing durable admission request and independent current feasibility/source
fence before obtaining a real reservation. Existing payment-first financial authority remains.

Source-head invalidation transactionally coalesces a refresh request per affected region. It
covers booking/reservation/payment acceptance, resource identity/availability/shifts, routing,
facilities and custody through existing source triggers. Refreshes run in existing finite
admission/regional execution; regional dispatch runs before advisory replacement maintenance.
`python -m tirodhan.workers.routing_availability` provides finite periodic reconciliation without
loading a road graph. It reads only due regions and is default-disabled with orchestration.
No scheduler, always-on service or cloud resource is provisioned by this increment.

The read model uses the existing seven-date, 30-minute operating grid. A refresh rebuilds only
the affected region's bounded upcoming grid, not every region or any matrices. Resource changes
can affect multiple later slots, so narrow invalidation must not assume only the pickup bucket
changed. Expired rows are pruned; interrupted refresh leaves its durable request recoverable.
Source-head locking and REPEATABLE READ prevent publishing mixed-source aggregates. A concurrent
source change retries; stale results cannot replace a newer source interpretation silently.

Database aggregation is proportional to matching regional records; the interval sweep is
O((V+B+S) log(V+B+S)) for vehicles, break endpoints and slot endpoints. GET performs indexed
regional reads. This is not a constant-time solution of the pickup-and-delivery routing problem.

## Road acquisition and calendar correctness

OSRM Table rectangles retain exact directed durations/distances and graph/profile/snap provenance.
Sparse stars use two requests, one-to-many and many-to-one; dense useful blocks retain square
requests to avoid turning a dense local repair into many HTTP requests. For a width-b star,
provider cells change from b² to 2b. Duplicate physical coordinates may share road arcs while
every booking retains its own stop, service duration, pickup window and custody identity.

Both paths use identical canonical snapping-token encoding. Negative/nonfinite values, inconsistent
reachability, excessive snapping, response limits, timeouts and provider fallback remain rejected.
No partial star is cached when its second rectangle fails or is cancelled. No distances are
clamped. The previously reproduced OSRM 6.0 phantom negative-distance defect remains an upstream
release blocker, with its native rejection/recovery tests retained.

Wider cached facility calendars are intersected with the actual routing horizon before conversion.
Entirely past openings cannot create an invalid [0,negative] interval. Actual closures, restrictions
and dated openings remain authoritative. Operational horizon_seconds and the existing separate
24-hour commercial certification horizon are unchanged. Same-location appointments several hours
apart remain distinct; proximity in time merely permits a feasibility search.

Validation additionally exposed an existing insertion defect: an unused empty vehicle with an
impossible immediate return aborted healthy alternatives. The independent contract requires no
journey for an unused vehicle. Empty schedule preparation now follows that rule, while every
inserted pickup/discharge/end path still receives exact calendar checks and the independent gate.
The new regression was observed failing before correction.

## Schema and integration

Migration `0031_regional_capacity_recovery` adds:

- `operating_region_vehicle.version`;
- immutable `region_capacity_transfer`;
- `rider_replacement` with recommendation/decision/version/retry state;
- immutable `rider_replacement_decision`;
- `regional_slot_projection` and coalesced `regional_slot_refresh`;
- ownership, shift-close and source-head refresh triggers/functions.

Existing custody/evidence/financial tables are not replaced. Downgrade refuses to discard
capacity-transfer/replacement audit history. An empty new feature schema can round-trip while
preserving populated financial history and the three initiation-term triggers. Application
metadata models the new tables/column; PostGIS extension ownership remains handled by the
existing strict schema-drift assertion rather than a new broad exclusion.

Default-disabled V2 HTTP integration:

- existing rider/manager `/v2/.../operations` discriminated command APIs;
- GET `/v2/manager/regions/{region_id}/capacity` for paged version/ownership references;
- GET `/v2/manager/replacements/{replacement_id}` for manager review;
- GET `/v2/customer/slot-availability?serviceability_context_id=...` for owned-context advisory slots.

Authorization uses existing live identity/role patterns. Commands use actor/client UUID,
fingerprint, transactional replay record and conventional result identifiers. Native work
has no assignment authority and runs outside business row locks. Source version, booking state,
resource authority and physical facts arbitrate concurrent execution, not the advisory UI.

## References and limits

- [OSRM 6.0 Table API](https://github.com/Project-OSRM/osrm-backend/blob/v6.0.0/docs/http.md):
  rectangular sources/destinations and directed duration/distance results.
- [OR-Tools VRPTW](https://developers.google.com/optimization/routing/vrptw): explicit visit windows.
- [Uber H3](https://www.uber.com/us/en/blog/h3/): geographic indexing, not evidence for a particular
  Tirodhan region size or dispatch ownership policy.
- [ORTEC Time Slot Booking](https://ortec.com/products/apps-and-services/time-slot-booking):
  commercial precedent for route/capacity-aware slot management.
- [Timefold recommended fit](https://timefold.ai/blog/fast-planning-optimization-with-the-recommended-fit-api):
  incremental feasible placement and human selection.

No new dependency or production infrastructure is introduced. Existing per-profile finite OSRM
hosting can still duplicate graph memory across concurrently executing containers. Region sizing,
whole-city road graph resources, production calibration and hosting must be measured before launch.
Spatial partitioning reduces ordinary working sets but cannot bound arbitrary demand density.
Sparse acquisition still has quadratic cumulative worst cases, and existing proof/history
amplification is not solved by this read model. No retention redesign or unsafe proof reuse is added.

Validation and controlled benchmark results are recorded in
[REGIONAL_CAPACITY_VALIDATION.md](REGIONAL_CAPACITY_VALIDATION.md) with their actual execution environment. They are fixture-scale observations, not Delhi-wide throughput or delivery SLAs.
