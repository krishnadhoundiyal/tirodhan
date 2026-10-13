# ADR-006: Rider Assignment and Operational Escalation

**Status:** Accepted for MVP

## V2 amendment — approved 12 October 2026

For V2, optimized authoritative dispatch prioritizes committed operating capacity,
including continuously collecting vehicles. During an agreed active operating shift,
an eligible committed rider may receive system-authorized assignments without accepting
each pickup. Fleet membership alone establishes neither commitment nor dispatch authority.
Flexible riders remain supplementary capacity through voluntary offers/acceptance,
activated proactively when committed resources cannot reliably satisfy commitments.
Future bookings do not require a currently available or assigned rider.

V2 eligibility uses actual vehicle capacity, onboard cargo, current position, shift,
existing ownership, protected stops, customer windows and authorized receiving points.
IDLE does not imply empty cargo; BUSY alone does not exclude feasible additions. Pickup
completion and cargo custody remain separate. Multiple pickup groups may share a feasible
continuous vehicle route without discarding stable per-household execution or assignment
history. Specified partial discharge must conserve quantitative cargo, not reset capacity.

The hierarchy and locking rules below describe retained V1 execution until cutover;
fleet-first voluntary group assignment is superseded as the V2 target.
Increment 1 adds no dispatch mutation or replacement locking order. It implements pure
proposal feasibility and retains COMMITTED/FLEXIBLE classification. Later transactional
dispatch must recheck live authority, resource revisions, ownership, cargo and facility
facts in PostgreSQL; an optimizer assessment alone never wins an assignment. Operations
and recovery preserve collected work and historical custody. Unmanned-kiosk V2 discharge
requires cargo/order QR or barcode, photographic evidence, trustworthy timestamps and
identifiable receiving point; geofencing alone is insufficient. Existing V1 evidence and
media-upload independence remain compatible. See [increment boundary](../V2_ROUTING_INCREMENT_1.md).

Increment 2's real-engine adapters preserve classification, hard eligibility, ownership,
protected prefixes and quantitative cargo without any dispatch mutation. A returned
feasible route is an optimization recommendation; it is neither committed-shift authority
nor flexible acceptance. No V1 IDLE or handover transition changes. Live operating shift,
position/cargo projection and transactional revision/authority fencing remain prerequisites
to assignment integration. See [implemented adapters](../V2_ROUTING_INCREMENT_2.md).

## Increment 3 — operational resource and custody projection

Manager-recorded shifts/attestation references distinguish committed and flexible inputs;
fleet membership is not shift authority. Only committed inputs enter the first shadow
evaluation stage. Current BUSY/IDLE/intent does not remove their obligations. Live rider
identity/role, explicit capacities, fresh positions and physical reservations are required.
Uncollected assignment ownership becomes hard vehicle binding. Successful PickupAttempt
attribution plus explicit legacy vehicle observation preserves unhanded cargo after IDLE.
Only validated historical whole-lot handover releases that projection. It is not new V2
kiosk/partial-discharge permission, and input registration does not authorize assignment.

Transactional route revisions, protected execution prefixes, coordinated resource locks,
committed dispatch and supplementary flexible acceptance remain the next increment.
V1 execution does not take the new resource lock, so the input checks/fingerprints cannot
yet arbitrate dispatch races against it. New V1 offers/assignments retire at the approved
single-owner V2 cutover; stable PickupExecution, collected work, custody and historical
assignments remain. See [implementation and transition](../V2_ROUTING_INCREMENT_3.md).

## Increment 4 — committed dispatch and vehicle custody

COMMITTED_DISPATCH is now a conventional audited assignment source bound to a currently
active manager-attested shift and vehicle through VehicleRoute. It requires no per-pickup
offer acceptance or AVAILABLE/IDLE intent. One route assignment can gain additional
independently feasible work while BUSY; current ownership and cargo remain hard constraints.
The source evaluation alone still has no authority: only the live activation transaction
may establish it. Flexible voluntary supplementary acceptance remains a separate next stage.

Ordered rider commands bind actual collection to its vehicle and performing assignment.
Final pickup does not return a routed rider to IDLE; pending discharge keeps its route/
cargo obligation. Whole-lot V2 discharge requires cargo/receiving identifiers, PHOTO capture
business facts, server-timestamp bounds, authorized opening/deadline and PostGIS geofence.
Rejected geofences remain durable without releasing cargo; upload remains independent.
Partial discharge, custody transfer and physical return-arrival are not implemented here.

Recovery releases only uncollected ownership and retains original collected attribution.
New revision commands protect started work; legacy pickup/handover/group reassignment paths
cannot bypass route authority. Released V2 work stays out of V1. Role revocation does not
prevent customer cancellation or manager recovery. Default API activation is false; mobile
task/label/photo integration and global operational cutover still require review.
See [transactions, compatibility and remaining gaps](../V2_ROUTING_INCREMENT_4.md).

## Decision (retained V1 execution; retire group dispatch at V2 cutover)

Assignment is by planned collection group/work, while per-household PickupExecution remains the stable fulfilment object.

### Rider state

Separate rider availability intent from platform work state.

Rider-controlled intent:

- `OFFLINE`
- `AVAILABLE`

Platform-controlled work state:

- `IDLE`
- `RESERVED`
- `BUSY`

Only an approved/active rider with `AVAILABLE` intent, `IDLE` work state, and required service/vehicle/capacity/slot eligibility may receive or win work.

Completing work returns platform work state without overriding the rider's chosen availability intent.

### Assignment hierarchy

1. create a complete eligible active-fleet offer cohort for the group's H3 resolution-7 cell;
2. if no fleet rider is eligible, create the independent cohort immediately as round 1;
3. notify the complete active-device set asynchronously with concurrent FCM push;
4. first valid synchronous HTTP acceptance wins atomically;
5. expired fleet offers without assignment request one next-round independent cohort;
6. expired independent offers leave manager/manual assignment as fallback;
7. manager performs manual assignment/reassignment; the selection judgment is outside optimization.

Fleet-first is opportunity ordering, not automatic fleet assignment. Fleet and independent
acceptance both use source RIDER_OFFER_ACCEPTED and the existing synchronous assignment transaction.
Only offer rows retain audience/fleet provenance; RiderProfile and RiderAssignment do not.
Fresh cohort riders must have ACTIVE AppUser, live RIDER role, ACTIVE RiderProfile, AVAILABLE
intent and IDLE work state under the existing authorization locks. Fleet riders additionally
require ACTIVE fleet/current membership/active matching fleet coverage. Independent riders require
matching rider coverage and no current membership, even in an inactive fleet.

The collection-group row lock serializes cohort creation, acceptance, manual assignment and timeout.
Cohorts share exact timestamps/round; fleet/coverage mutations lock fleet before rider and cohort
locks sort fleet/rider IDs. Planning's normal and fallback result transaction emits one group/stage
outbox event; Service Bus transports notification work, not per-rider offers or acceptance commands.
The consumer commits offers and devices before FCM. PROCESSING resumes pending deliveries; push
may duplicate after ambiguity and is best-effort/at-least-once. PostgreSQL owns assignment truth.
There is no orchestration, saga, acceptance worker or change to HTTP ownership confirmation.

### Historical reassignment

PickupExecution is not replaced when assignment changes.

If a rider cannot continue:

- completed pickups remain completed;
- only outstanding pickup executions are released/reassigned;
- predecessor assignment remains historical;
- successor assignment is created for residual work.

Critical assignment races are arbitrated by PostgreSQL transaction/constraints, not a read-then-write check in application code.

For the Phase 1H initial-assignment slice, assignment is full-group and has only `ACTIVE` status.
Offer acceptance and manager assignment require an `ACTIVE`, `AVAILABLE`, `IDLE` rider and share
one transaction. The group row and active-assignment partial unique index serialize group
ownership; the rider-availability row serializes assignment of one rider; unreleased
`rider_assignment_item` rows are authoritative for pickup ownership. Offer expiry remains
timestamp-based. Fleet persistence is introduced in Phase 1U; reassignment follows Phase 1J below.

For Phase 1I, assignment start is recorded by `started_at` and moves rider work state
`RESERVED -> BUSY`; `OFFLINE` intent does not cancel existing work. Pickup attempts retain the
performing assignment identity. A successful attempt atomically marks its pickup `COLLECTED`.
When every unreleased pickup owned by the assignment is collected, the assignment becomes
`COMPLETED` and rider work state returns `BUSY -> IDLE` without changing intent or releasing
assignment items.

Phase 1J adds terminal assignment status `SUPERSEDED` for a predecessor whose residual uncollected
work moved to a manager-created successor. Collected work remains attached to the predecessor;
only `ASSIGNED` items are released with reason `REASSIGNED` and recreated under the successor.
The predecessor rider returns to `IDLE`, the eligible replacement becomes `RESERVED`, and optional
incident resolution is atomic with transfer. Reassignment uses `idempotency_record`, emits no
outbox event in this phase, and preserves both assignments and immutable pickup-attempt history.

## Possible exception resolutions

- retry;
- reassign;
- cancel;
- cancel + refund where applicable.

Active-pickup exceptions may use a dedicated WhatsApp Business support channel. Tirodhan remains the system of record for the resolution.

## Open

- exact fan-out deadline.

There is no fleet scoring/selection algorithm in Phase 1U: every currently eligible cell member
receives an offer. Future capacity/vehicle/slot policies are not invented by this implementation.
