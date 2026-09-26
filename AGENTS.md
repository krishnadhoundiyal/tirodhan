# Agent Instructions

These rules apply to all coding and infrastructure agents working in this repository.

## 1. Architecture authority and context loading

Always read:

1. `docs/PROJECT_CONTEXT.md`
2. `docs/ARCHITECTURE.md`

Then read the documents relevant to the task rather than loading the entire documentation set by default:

- security, authentication, PII, media privacy -> `docs/DATA_PROTECTION.md`;
- domain/entity/lifecycle work -> `docs/DOMAIN_MODEL.md`;
- database/migration/constraint work -> `docs/SCHEMA_DESIGN.md` and, where relationships matter, `docs/ER_DIAGRAM.md`;
- any mutating API, webhook, worker, scheduler, concurrency or retry work -> `docs/IDEMPOTENCY.md`;
- infrastructure/platform/provider decisions -> the relevant ADR(s);
- any feature spanning several domains -> all documents relevant to those domains.

Read the relevant ADR(s) for every implementation task.

Do not read every detailed document merely for orientation when the task does not touch it. Conversely, do not omit a document to save context when it can materially affect correctness.

If uncertain whether a document is relevant, read it. If implementation requires an architectural choice that is not documented or is explicitly open, stop and surface the decision instead of making it implicitly.

The documented architecture and domain model are human-approved. Do not silently replace, reinterpret, or "improve" an architectural decision.

## 2. Standard model before bespoke model

Do not invent a Tirodhan-specific structure where an established application pattern already fits.

Use the approved conventional patterns for:

- application identity, verified phone, roles, sessions;
- multi-address customer profile;
- order/booking and line items;
- logical payment, payment attempts, provider events, refunds;
- dispatch offers and assignments;
- fulfilment/pickup execution and attempt history;
- facility/receiving-point master data;
- evidence/attachment metadata.

The geographic planning/compaction workflow is materially product-specific. Even there, use standard persistence, job, idempotency, and concurrency patterns.

## 3. Agent freedom

Agents are expected to choose good implementation details where the architecture specifies an invariant rather than a mechanism.

Examples:

- choose suitable SQL/ORM mechanics for an atomic transition;
- choose suitable row-locking/conditional-update mechanics for concurrency;
- choose suitable bounded cursor/page/batch mechanics;
- choose a clean implementation for an approved unique/business constraint.

Do not require architecture documents to prescribe every `if` statement, SQL statement, or class.

## 4. Prohibited architectural drift

Do not:

- replace Azure Container Apps with App Service, AKS, Functions, VMs, or another compute platform without an ADR change;
- replace Azure Service Bus Standard with another broker without an ADR change;
- replace PostgreSQL/PostGIS with MongoDB, Cosmos DB, or another database without an ADR change;
- add Redis or another cache merely for convenience;
- add runtime LLM/AI dependency to geographic compaction;
- split the transactional API into independently deployed microservices without an ADR change;
- merge independently scaled workers into the synchronous API for convenience;
- place business authorization rules only in APIM;
- introduce Application Insights, Log Analytics, or Diagnostic Settings as the default logging path;
- weaken data-protection requirements;
- introduce a paid external service without surfacing the cost/need first;
- create permanent Azure dev/qa/staging environments without an ADR change;
- replace Terraform as the IaC tool without an ADR change;
- add Azure Container Registry while GHCR works unless a concrete Azure integration/operational issue justifies it;
- make production deployment automatic without the required production approval;
- replace approved relational entities with generic entity/type/value or polymorphic-reference tables merely for convenience;
- make mutable profile/master data authoritative for already accepted historical transactions.

## 5. Business invariants

Preserve these invariants:

- a paid and accepted collection request is a fulfilment commitment;
- mutable profile/master data must never rewrite historical transaction facts;
- geographic compaction is an optimization, not a condition of fulfilment;
- a request with no compactable neighbour becomes a singleton collection unit, not a failed request;
- technical compaction failure must never silently drop a request;
- all valid `PRE_PLANNING` requests must ultimately become `PLANNED` unless infrastructure prevents persistence;
- completed pickup work is never rolled back because later pickups in the same assignment fail;
- `PickupExecution` is the stable per-household fulfilment object; assignments may change around it;
- media upload failure does not reverse a successfully performed pickup/handover;
- evidence capture is a business fact; media upload is a storage operation;
- one logical Payment belongs to a CollectionRequest; gateway retries are PaymentAttempts;
- Refund is a separate financial lifecycle;
- human identity is used for application authorization/audit; Azure-resource access uses workload identity;
- no business rule may depend on APIM being permanently present.

## 6. Idempotency is mandatory

Every state-changing operation that can be retried, replayed, duplicated, redelivered, timed out, or executed concurrently must have an explicit idempotency strategy.

Use all applicable layers:

1. client/API command idempotency;
2. database/domain uniqueness and transactional constraints;
3. message inbox + transactional outbox;
4. external-provider idempotency or reconciliation.

Transport deduplication never replaces business idempotency.

No new mutating endpoint, webhook, worker, scheduled job, mobile mutation, or external side-effect integration is complete until its documented idempotency behaviour is implemented and tested.

For every such operation identify:

- logical/business key;
- DB protection;
- transaction boundary;
- replay result;
- concurrency rule;
- external side-effect rule.

Follow `docs/IDEMPOTENCY.md`.

## 7. Testing expectations

Tests should demonstrate invariants, not mirror implementation.

Where applicable, every mutation must test:

- normal success;
- exact replay;
- duplicate message delivery;
- concurrent execution;
- crash/retry around DB commit;
- crash/retry around external provider side effects.

Examples:

- duplicate planning-batch delivery produces no duplicate groups or events;
- exhausted compaction attempts produce fallback singleton planning;
- cancellation and planning freeze racing produce one valid winner;
- two riders concurrently accepting the same group yield one assignment;
- completed pickups survive reassignment of outstanding work;
- duplicate payment/refund webhook does not repeat business effects;
- late duplicate external payment success is recorded and reconciled rather than re-accepting the request;
- missing precomputed serviceability uses the same synchronous domain operation;
- duplicate evidence/media/handover mobile submission maps to the same logical record.

## 8. Security and sensitive data

Follow `docs/DATA_PROTECTION.md`.

Never place secrets, OTPs, payment instrument data, raw tokens, customer addresses, phone numbers, or exact coordinates in logs.

Service Bus, inbox/outbox, idempotency metadata, and provider-event metadata must not become accidental PII stores. Prefer identifiers and minimal routing data.

## 9. Database rules

Follow `docs/SCHEMA_DESIGN.md`.

In particular:

- use real FKs and approved uniqueness constraints;
- use DB transactions/conditional state transitions to arbitrate races;
- do not substitute Python read-then-write checks for DB-enforced critical invariants;
- preserve append-only history where the domain requires it;
- use PostGIS for canonical spatial data;
- do not use floating point for money;
- do not introduce universal soft-delete flags;
- do not use JSONB to avoid modelling core relational concepts.

## 10. Cost discipline

This is a founder-funded MVP. Cost is an explicit architectural constraint.

Prefer scale-to-zero and already-selected managed primitives. Do not add infrastructure "just in case."

If an implementation materially increases recurring cost, surface the impact before implementation.

## 11. Environment, recovery and delivery rules

- Permanent environments are local, Azure `nonprod`, and Azure `prod`.
- Additional staging is ephemeral and created from Terraform only when required.
- Production data is not copied directly into nonprod.
- PostgreSQL recovery uses native PITR with the documented RPO/RTO objectives.
- Restore procedures must be periodically tested.
- CI/CD provider may be Azure DevOps Pipelines or GitHub Actions until explicitly frozen.
- CI/CD should use workload federation/OIDC where supported instead of long-lived Azure deployment credentials.
- GHCR is the default container registry; ACR is a fallback only for a concrete Azure-specific issue.
