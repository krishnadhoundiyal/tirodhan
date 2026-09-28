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

1. use available fleet capacity for the cell + slot where suitable;
2. otherwise fan out to eligible independent riders;
3. first valid acceptance wins atomically;
4. if no acceptance arrives by the configured deadline, escalate to manager;
5. manager performs manual assignment/reassignment; the rider-selection judgment itself is outside software optimization.

Fleet auto-assignment, independent acceptance, and manual assignment all create the same durable RiderAssignment type.

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
timestamp-based. Fleet persistence and reassignment semantics are deferred.

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

- exact fleet rider-selection algorithm;
- exact fan-out deadline.
