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

## Possible exception resolutions

- retry;
- reassign;
- cancel;
- cancel + refund where applicable.

Active-pickup exceptions may use a dedicated WhatsApp Business support channel. Tirodhan remains the system of record for the resolution.

## Open

- exact fleet rider-selection algorithm;
- exact fan-out deadline.
