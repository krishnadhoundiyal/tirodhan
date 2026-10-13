# ADR-004: Geographic Planning and Compaction

**Status:** Accepted

## V2 amendment — approved 12 October 2026

The cell/slot compaction decision below describes retained V1 execution for its transition
and historical batches. V2 supersedes it as the target workflow, with OSRM road matrices,
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

## Increment 3 — operational inputs and shadow execution

Durable shifts, physical capacity/load reservations, timestamped PostGIS positions and
legacy cargo observations now feed one consistent PostgreSQL snapshot of actual bookings,
incumbent ownership and custody. The same cross-cell/cross-slot problem is persisted and
evaluated by both real engines behind the independent gate. Known source drift invalidates
the advisory result. No booking freezes, group construction or dispatch occur. Missing
physical facts fail readiness with visible scope IDs; accepted work remains pending.

This is a prerequisite to a **single V2 operational workflow**. The next integration must
commit route revisions, bind execution/vehicle custody and arbitrate cancellation under
live locks. At approved cutover, new V1 batch discovery/greedy grouping and group-bound
dispatch must retire; historical batches drain without rewriting accepted facts. Operational
projection cadence remains independent of customer cancellation rights. No production
activation/road-graph hosting is selected. See [implemented boundary and cutover](../V2_ROUTING_INCREMENT_3.md).

## Increment 4 — transactional route activation

The separate activation transaction now revalidates either pinned engine against current
bounded PostgreSQL sources and elapsed time under SERIALIZABLE isolation and resource/
ownership locks. It commits actual committed assignments and immutable route revisions.
V2 execution uses stable household pickups without synthetic cell batches/groups, preserves
STARTED prefixes and permits feasible additions to loaded BUSY vehicles. Advance allocation
does not freeze ACCEPTED booking rights. H3/work-unit advisory locking remains only the
transition arbitration boundary with cancellation/V1 freeze, not a route construction limit.

V2-admitted work is excluded from V1 freeze and fresh group dispatch, including after
recovery release. Active V1 assignments and PRE_PLANNING batches drain before import.
Global single-owner rolling admission, flexible supplementation and notification delivery
remain the next increment; this is no permanent V1 fallback. Default activation is false,
with no production hosting selected. See [implemented authority](../V2_ROUTING_INCREMENT_4.md).

## Decision (retained V1 execution; retire new admission at V2 cutover)

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

## Approved V2 implementation progression: Increment 5

The preceding cell/batch algorithm describes retained V1 history and compatibility.
V2 replacement planning jointly considers bounded city-wide pending demand across
cells/slots with current committed resources and cargo. It uses pinned OSRM, VROOM
and OR-Tools, independent feasibility and explicitly calibrated measured economic
selection. Optional debt is retained with durable exceptions/retry; native total failure
does not drop bookings or dispatch an unreviewed heuristic. This supersedes cell-local
optimization for newly admitted V2 work when approved cutover occurs. Permanent
parallel dispatch is not the target. Production activation and global V1 retirement
remain separate release gates. See [Increment 5](../V2_ROUTING_INCREMENT_5.md).

## Increment 6 verification correction

An independently shrunk example showed that a single refinement sweep could retain
an unnecessary second vehicle after adding later work. Feasible consolidation/insertion
now repeats to a strict local fixed point or the existing total candidate limit. Both
engines, independent feasibility, objective calibration, authority and infrastructure
remain as approved. No global-optimum guarantee is implied. Test-only generated
verification/evidence is documented in [Increment 6](../V2_ROUTING_INCREMENT_6.md).
