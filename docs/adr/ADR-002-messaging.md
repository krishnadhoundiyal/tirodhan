# ADR-002: Messaging

**Status:** Accepted for MVP

## Decision

Use Azure Service Bus Standard.

Use separate logical entities by business responsibility rather than one generic queue.

Transport is at-least-once.

Consumers must implement both:

- transport-level durable inbox/message deduplication where applicable;
- domain/business idempotency using the actual business key.

A transactional outbox is used where a PostgreSQL state change must reliably cause an asynchronous event.

Geo-planning work is serialized by geographic cell; Service Bus sessions are the preferred mechanism for one-active-owner-per-cell while permitting concurrency across cells.

Use Peek-Lock semantics for work that must not be lost before durable completion.

Messages carry identifiers/control data rather than full business datasets or PII-rich objects.

## Rationale

Service Bus Standard provides the production messaging features required by planning, notification, refund and other asynchronous workflows without requiring Premium.

Inbox/outbox plus domain idempotency addresses the real failure boundaries:

- duplicate/redelivered broker messages;
- process crash after DB commit but before message settlement;
- DB commit followed by delayed event publication;
- outbox publisher crash after sending but before marking published.

## Consequences

- PostgreSQL remains authoritative;
- Service Bus is not a backup/data store;
- Service Bus duplicate detection does not replace application/business idempotency;
- DLQ is an infrastructure safety net, not a normal business fallback;
- message settlement occurs only after the corresponding durable DB outcome is committed;
- an outbox event may be delivered more than once, so consumers remain idempotent;
- the contract is at-least-once transport with exactly-once intended business effect, not distributed exactly-once execution.

See `docs/IDEMPOTENCY.md` and ADR-014.
