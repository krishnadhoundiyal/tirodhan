# ADR-004: Geographic Planning and Compaction

**Status:** Accepted

## V2 amendment — approved 12 October 2026

The cell/slot compaction decision below describes V1 execution and remains authoritative
for V1 batches. V2 dynamic collection optimization is approved with OSRM road matrices,
VROOM new-work construction and OR-Tools committed-route suffix optimization behind one
versioned contract and independent feasibility gate. PostgreSQL/domain state remains
authoritative; a solver result is a proposal, not assignment or custody permission.

V2 may plan across H3 cells and compatible pickup slots. H3 remains the resolution-7
serviceability index, not an economic route boundary. Advance provisional planning has
operational lead time independent of cancellation cutoff; projecting future accepted
bookings must not freeze them or change financial/cancellation rights. Accepted windows
are hard service-start promises. Preserve immutable source batch/booking facts and stable
PickupExecution history when later adding route revisions. Optimization failure cannot
silently drop work or erase an incumbent commitment.

Increment 1 implements the pure proposal contract and validator, including capacity,
initial cargo, protected stops and specified whole/partial authorized discharge. V1's
scheduler, freeze, grouping and singleton fallback are unchanged. No routing runtime or
new infrastructure is enabled. See [implementation boundary](../V2_ROUTING_INCREMENT_1.md)
for contract limits, integration prerequisites and remaining operational configuration.

Increment 2 adds bounded OSRM Table acquisition and pinned VROOM/OR-Tools child-process
adapters under that unchanged contract/gate. Prefixes are advanced before suffix solving
and checked again in full; native outputs/costs cannot establish mandatory feasibility.
Both engines support whole new pickups and specified partial discharge of existing cargo;
OR-Tools additionally supports specified split discharge of new pickups. VROOM rejects
that unsupported projection explicitly. The engineering duration/optional-coverage
objective is not a production economic policy. No normal batch or worker invokes V2,
and no production graph hosting/resource is selected. See [increment 2](../V2_ROUTING_INCREMENT_2.md)
for capabilities, measured validation, finite recovery and hosting tradeoffs.

## Decision

Customers book 30-minute pickup slots.

A scheduled planning job runs a configurable lead time `N` before each slot.

The work unit is an immutable planning batch for a `(cell, slot)`, not an individual request.

Eligible `ACCEPTED` requests are frozen atomically into the batch and move to `PRE_PLANNING`.

One logical compaction owner processes a cell batch at a time. The worker uses bounded/iterable access to PostgreSQL and may materialize a minimal spatial working set if the chosen clustering algorithm requires it.

Compaction produces collection groups:

- neighbours => shared group;
- no neighbours => normal singleton group.

The first approved algorithm is `BOUNDED_GREEDY_DIAMETER_V1`: deterministic greedy grouping with UUID-stable seed selection, PostGIS-distance/UUID candidate ordering, all-pairs distance compatibility, and a snapshotted maximum household-stop count. Algorithm version, distance in meters, and group-count bound are immutable planning-batch snapshots reused by every logical attempt.

PostGIS geography is the distance authority. Phase 1G does not use declared item weight/quantity, inferred physical capacity, routing, or cross-cell candidates. ADR-015 freezes upstream cells as canonical H3 resolution-7 indexes.

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

- values of `N` and `P`.
- production compaction-distance and maximum group-request values.
