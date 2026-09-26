# ADR-004: Geographic Planning and Compaction

**Status:** Accepted, with algorithm/cell-size details open

## Decision

Customers book 30-minute pickup slots.

A scheduled planning job runs a configurable lead time `N` before each slot.

The work unit is an immutable planning batch for a `(cell, slot)`, not an individual request.

Eligible `ACCEPTED` requests are frozen atomically into the batch and move to `PRE_PLANNING`.

One logical compaction owner processes a cell batch at a time. The worker uses bounded/iterable access to PostgreSQL and may materialize a minimal spatial working set if the chosen clustering algorithm requires it.

Compaction produces collection groups:

- neighbours => shared group;
- no neighbours => normal singleton group.

There is no request-level compaction failure outcome.

Logical compaction attempts are explicit business/operational attempts and are not the same thing as Service Bus DeliveryCount/redelivery.

Technical failures retry the same immutable batch. After configurable maximum compaction attempts `P`, the batch deliberately degrades to fallback singleton planning for every remaining request.

Successful durable planning creates groups/membership and stable per-request PickupExecution records, moves requests to `PLANNED`, completes the batch, and records required outbox event(s) in one transaction.

Runtime generative AI is not used for clustering.

## Invariants

- all valid `PRE_PLANNING` requests ultimately become `PLANNED` unless infrastructure prevents persistence;
- duplicate/redelivered work must not duplicate groups, pickup executions or downstream events;
- late/new requests do not silently join an existing immutable batch;
- one request belongs to one planning group in the completed planning result;
- compaction is an optimization, not fulfilment eligibility;
- exhaustion of optimization attempts produces valid fallback singleton fulfilment, not customer failure;
- infrastructure failure that prevents persistence remains recoverable and must not be disguised as successful fallback.

## Open

- physical cell sizing;
- clustering algorithm;
- values of `N` and `P`.
