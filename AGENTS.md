# Agent Instructions

These rules apply to all coding and infrastructure agents working in this repository.

## 1. Architecture authority

Read, in order:

1. `docs/PROJECT_CONTEXT.md`
2. `docs/ARCHITECTURE.md`
3. `docs/DATA_PROTECTION.md`
4. relevant files in `docs/adr/`

The documented architecture is human-approved. Do not silently replace, reinterpret, or "improve" an architectural decision.

If implementation requires an architectural choice that is not documented, stop and surface the decision instead of making it implicitly.

## 2. Agent freedom

Agents are expected to choose good implementation details where the architecture specifies an invariant rather than a mechanism.

Examples:

- if a planning operation must be idempotent, choose an appropriate implementation;
- if a request transition must be atomic, choose suitable transactional SQL/ORM mechanics;
- if a worker must use bounded access to data, choose an appropriate cursor/page/batch implementation.

Do not require the architecture documents to prescribe every `if` statement, SQL statement, or class.

## 3. Prohibited architectural drift

Do not:

- replace Azure Container Apps with App Service, AKS, Functions, VMs, or another compute platform without an ADR change;
- replace Azure Service Bus Standard with another broker without an ADR change;
- replace PostgreSQL/PostGIS with MongoDB, Cosmos DB, or another database without an ADR change;
- add Redis or another cache merely for convenience;
- add a runtime LLM/AI dependency to geographic compaction;
- split the transactional API into independently deployed microservices without an ADR change;
- merge independently scaled workers into the synchronous API for convenience;
- place business authorization rules only in APIM;
- introduce Application Insights, Log Analytics, or Diagnostic Settings as the default logging path;
- weaken data-protection requirements;
- introduce a paid external service without surfacing the cost/need first;
- create permanent Azure dev/qa/staging environments without an ADR change;
- replace Terraform as the IaC tool without an ADR change;
- add Azure Container Registry while GHCR is working unless a concrete Azure integration/operational issue justifies it;
- make production deployment automatic without the required production approval.

## 4. Business invariants

Preserve these invariants:

- a paid and accepted collection request is a fulfilment commitment;
- geographic compaction is an optimization, not a condition of fulfilment;
- a request with no compactable neighbour becomes a singleton collection unit, not a failed request;
- technical compaction failure must never silently drop a request;
- Service Bus processing is at-least-once; workers must be idempotent;
- completed pickup work is never rolled back because later pickups in the same rider assignment fail;
- media upload failure does not reverse a successfully performed pickup;
- refund processing is tracked independently from collection-request state;
- human identity is used for application authorization/audit; Azure-resource access uses workload identity;
- no business rule may depend on APIM being permanently present.

## 5. Testing expectations

Where relevant, tests should demonstrate invariants rather than mirror implementation.

Examples:

- duplicate delivery of the same planning batch produces no duplicate business result;
- a failed compaction attempt leaves its immutable batch recoverable;
- exhausted compaction retries produce singleton planning outputs;
- two riders accepting the same collection group cannot both win;
- cancellation cannot race successfully after the request has entered `PRE_PLANNING`;
- previously completed pickups remain completed after rider reassignment of outstanding work;
- payment/refund webhook redelivery is idempotent;
- missing precomputed serviceability falls back to synchronous resolution.

## 6. Security and sensitive data

Follow `docs/DATA_PROTECTION.md`.

Never place secrets, OTPs, payment instrument data, raw tokens, customer addresses, phone numbers, or exact coordinates in logs.

Do not put sensitive personal data in Service Bus messages unless technically necessary. Prefer identifiers and load authoritative data from PostgreSQL.

## 7. Cost discipline

This is a founder-funded MVP. Cost is an explicit architectural constraint.

Prefer scale-to-zero and managed primitives already selected. Do not add infrastructure "just in case."

If a proposed implementation materially increases recurring cost, surface the estimate/impact before implementation.

## 8. Environment, recovery and delivery rules

- Permanent environments are local, Azure `nonprod`, and Azure `prod`.
- Additional staging is ephemeral and created from Terraform only when required.
- Production data is not copied directly into nonprod.
- PostgreSQL recovery uses native PITR with the documented RPO/RTO objectives.
- Restore procedures must be periodically tested.
- CI/CD provider may be Azure DevOps Pipelines or GitHub Actions until explicitly frozen.
- CI/CD should use workload federation/OIDC where supported instead of long-lived Azure deployment credentials.
- GHCR is the default container registry; ACR is a fallback only for a concrete Azure-specific issue.
