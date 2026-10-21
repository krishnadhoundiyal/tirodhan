# ADR-002: Messaging

## Increment 8 — regional durable wake-up

PostgreSQL stores one coalesced wake generation per configured region. Known booking/vehicle
changes target their region; broader configuration changes may wake configured owners.
Existing financial outbox facts are consumed idempotently without changing producers. A wake is
not an order-completion ledger. Relational booking outcomes, route authority and captured-generation
acknowledgment commit together; later generations and deferred retry debt survive. Finite deadline/
backlog probes remain necessary without new messages. No new queue, broker, always-on consumer or
cloud deployment is introduced. See [implemented recovery](../V2_ROUTING_INCREMENT_8.md).

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

## V2 route authority — increment 4

VehicleRouteRevised, VehicleRouteProgressed and VehicleRouteRecoveryRequired append
identifier-only outbox records with authoritative transactions. They are not registered
with the existing publisher/notification consumer and remain PENDING; no new queue or
infrastructure is provisioned. The next delivery increment must explicitly wire these
events and preserve inbox/send-before-publish recovery. V2 route authority is in PostgreSQL,
not a notification or per-cell Service Bus session. The cell-session preference above
describes retained V1 planning/drain, not a V2 optimization partition.

## Increment 5 fleet events

Route revision, recovery and fleet attention are identifier-only transactional outbox
facts. Their new production notification delivery consumer is not yet connected. Rider
polling and proven committed-task compatibility permit execution under delayed delivery.
No new broker or paid notification infrastructure is provisioned. See
[Increment 5](../V2_ROUTING_INCREMENT_5.md) for the remaining release integration.


## Recovery and revision-delivery integration

RecoveryWorkRequested uses an explicit identifier-only envelope on the existing planning queue with per-work sessions. The existing planning worker records an inbox and executes separately claimed work. The finite scheduler recreates due debt. Revision notifications are explicitly routed to the existing rider queue/consumer and request current-route refresh. No region-wide broker authority, new queue or paid infrastructure is introduced.

See [implemented contracts and release gates](../RIDER_FAILURE_RECOVERY.md).
