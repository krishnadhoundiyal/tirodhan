# ADR-002: Messaging

**Status:** Accepted for MVP

## Decision

Use Azure Service Bus Standard.

Use separate logical entities by responsibility rather than one generic queue.

Processing is at-least-once and consumers must be idempotent.

Geo-planning work is serialized by geographic cell; Service Bus sessions are the preferred mechanism for one-active-owner-per-cell while permitting concurrency across cells.

Use Peek-Lock semantics for work that must not be lost before durable completion.

## Rationale

Standard provides the production messaging features required by payment/refund, notification, and planning workflows without requiring Premium.

## Consequences

- messages are control/work signals, not the authoritative dataset;
- PostgreSQL remains authoritative;
- DLQ is an infrastructure safety net, not the normal business fallback;
- message settlement occurs after the corresponding durable business result has committed.
