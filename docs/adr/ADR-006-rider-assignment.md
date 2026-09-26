# ADR-006: Rider Assignment and Operational Escalation

**Status:** Accepted for MVP

## Decision

Assignment is by planned collection group.

Riders explicitly control `AVAILABLE` / `OFFLINE` intent.

Assignment hierarchy:

1. use available fleet capacity for the cell + slot where available;
2. otherwise fan out to eligible `AVAILABLE` independent riders;
3. first valid acceptance wins atomically;
4. if no acceptance arrives by the configured deadline, escalate to a manager;
5. manager performs manual rider selection; that selection decision is outside software optimization.

Pickup execution is tracked per household so partial progress is preserved.

If a rider cannot continue after completing part of a group, completed pickups remain completed and only outstanding pickups are reassigned.

Active-pickup exceptions may be escalated through a dedicated WhatsApp Business support channel. The manager records the resolution in Tirodhan.

## Possible resolution outcomes

- retry;
- reassign;
- cancel;
- cancel + refund where applicable.

## Open

- exact fleet rider-selection algorithm;
- exact fan-out deadline.
