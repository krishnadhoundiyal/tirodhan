# Manager business configuration: implementation inventory

Base `7892d64877d918f218e33b29c2b1fad491fbba49`, verified against fetched main and the
existing `codex/manager-business-configuration` branch on 20 October 2026. Clean checkout.
This inventory precedes implementation. Backend only; frontend repository is read-only.

## Reuse unchanged

- Existing catalogue identifiers/customer projections and private product-art read authorization.
- Booking PricingPort and immutable item/Payment snapshots; completed booking replay bypasses pricing.
- Resource/operational commands for vehicle creation, shift confirmation/extension/close,
  receiving windows/restrictions, regional creation, single-owner capacity transfer and fleet membership.
- Regional snapshot/problem construction, real road providers, both approved engines and independent
  feasibility validator; native work outside business transactions.
- SERIALIZABLE Payment-first activation, stable PickupExecution, immutable RouteRevision/stop history,
  protected task order, conventional assignment/item release history and regional wakes.
- Existing CargoTransferIntent/receipts/witness/CompleteTransfer; manager approval cannot move cargo.
- Existing client command idempotency, live role checks and transactional outbox. No new infrastructure.

## Extend

- Add manager catalogue/media metadata, receiving point and rider/profile/role administration,
  plus controlled edits missing from existing vehicle/region/fleet capabilities.
- Add versioned flat INR pickup price and its production PricingPort. Allocate the single charge
  deterministically across line items; existing booking and financial snapshots remain authoritative.
- Expose chronological, paginated revisions, individual versions and field comparisons. Use separate
  entity-specific revision tables/FKs for mutable master data, with transaction-attributed DB capture.
  Reuse existing route/assignment/shift/transfer lifecycle records for lineage, not another authority.
- Extend RiderReplacement with attributable manager initiation and FleetException incident grouping;
  preserve rider initiation. Failed resource recovery does not fabricate rider consent.
- Replace best-only advisory publication with immutable evaluation and selectable candidate records,
  source/expiry fencing, deterministic committed-first ranking and explicit UNKNOWN/exclusion metadata.
- Candidate feasibility must retain actual existing commitments, protected work, cargo and regional
  facility constraints. The current single-target implementation scopes by one vehicle; extend its
  source coverage/provenance instead of building a second planner.
- Selection identifies a published candidate, not an arbitrary rider. Retain explicit APPROVED/pending
  state until the existing regional activation commits assignment/route authority. After physical
  transfer, custody success and route activation must remain separately visible.

## Additive persistence / API work

Additive migrations after 0032: domain-specific immutable master revisions; versioned pickup pricing;
replacement evaluation/candidates and request/decision provenance, incident links and activation lineage.
Retain old rows as explicitly historical/unknown provenance; never invent actors for pre-migration edits.
History is not rollback. Populated history must block lossy downgrade. Media references are immutable
asset IDs/object identities, never expiring SAS identities or image bytes.

Manager APIs use the authenticated actor, bounded pages, expected versions and client UUIDs.
Mutations write live state, history, idempotency result and necessary identifier-only events atomically.
Existing routes/commands remain the only operational authority. No production flags are enabled.

## Verification

Focused disposable PostGIS tests: permissions/revocation, replay/conflict, concurrent/stale updates,
history immutability/comparison/media references, pricing/replay snapshots, migration upgrade/downgrade;
replacement candidates with other actual committed work, constraints, stale/concurrent selection,
before/after pickup, multi-lot transfer, pending vs activated state and native UNKNOWN/recovery.
Run repository lint/format/mypy and requested default pytest, separately reporting native opt-ins,
failures and skips. No unrelated final benchmark campaign. Commit, push and open a main-target PR;
do not merge or deploy.
