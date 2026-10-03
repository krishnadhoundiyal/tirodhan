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

## Phase 1U dispatch notifications

Normal/fallback planning appends CollectionGroupDispatchRequested transactionally per new group.
One message per group/stage (FLEET_FIRST or INDEPENDENT), never one per rider, carries group ID,
batch ID, canonical H3 cell, slot and stage only. The finite publisher adds this explicit route
to the configured rider notification queue; ServiceabilityRequested still actually sends to its
existing queue before PUBLISHED. No arbitrary-event route is introduced.

The Peek-Lock notification consumer prepares complete offer/device delivery rows in PostgreSQL,
calls Firebase Admin multicast outside transactions, then persists per-device results. Durable
PROCESSING inbox state resumes after crash/ambiguous send; only no remaining retryable delivery
allows PROCESSED then settlement. Permanent invalid tokens are revoked, transient failures retry.
Push is at-least-once/best-effort; duplicate notification is acceptable, duplicate assignment is not.
PostgreSQL owns assignment truth and synchronous HTTP acceptance still establishes ownership.
Service Bus transports notification work, with no saga/orchestrator/queued acceptance.
Workload identity remains the Azure-access contract. Runtime entry points do not freeze deployment
hosting/scheduling topology; no Terraform is introduced here.
