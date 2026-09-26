# ADR-004: Geographic Planning and Compaction

**Status:** Accepted, with algorithm/cell-size details open

## Decision

Customers book 30-minute pickup slots.

A scheduled planning job runs a configurable lead time `N` before each slot.

The work unit is an immutable planning batch for a `(cell, slot)`, not an individual request.

Eligible `ACCEPTED` requests are frozen into the batch and move to `PRE_PLANNING`.

One logical compaction owner processes a cell batch at a time. The worker uses bounded/iterable access to PostgreSQL and may materialize a minimal spatial working set if the chosen clustering algorithm requires it.

Compaction produces collection groups:

- neighbours => shared group;
- no neighbours => singleton group.

There is no request-level compaction failure outcome.

Technical failures retry the same immutable batch. After configurable maximum compaction attempts `P`, the batch degrades to singleton planning for every request.

Successful durable output moves requests to `PLANNED`.

Runtime generative AI is not used for clustering.

## Invariants

- all valid `PRE_PLANNING` requests must eventually become `PLANNED`;
- duplicate delivery must not duplicate planning results;
- late/new requests must not silently join an existing immutable batch;
- compaction is an optimization, not fulfilment eligibility.

## Open

- physical cell sizing;
- clustering algorithm;
- values of `N` and `P`.
