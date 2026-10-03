# ADR-006: Rider Assignment and Operational Escalation

**Status:** Accepted for MVP

## Decision

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
