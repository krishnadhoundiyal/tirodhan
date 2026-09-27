# Architecture

## 1. Architectural principles

1. Azure is the reference MVP cloud, not a hard domain dependency.
2. Design logical services cleanly, but deploy as few units as economics justify.
3. Use scale-to-zero compute where appropriate.
4. PostgreSQL is the authoritative transactional state store.
5. Service Bus is a work/event transport, not a data store.
6. At-least-once transport is expected; exactly-once intended business effect is achieved through idempotency and database invariants.
7. Every retryable, replayable, redeliverable, timeout-prone, or concurrent mutation requires an explicit idempotency strategy.
8. Prefer established application patterns before introducing bespoke Tirodhan-specific modelling.
9. Mutable profile/master data must never rewrite immutable historical transactional facts.
10. Geographic compaction is deterministic optimization logic, not runtime generative AI.
11. Human operational fallbacks are first-class parts of fulfilment.
12. Business history belongs in transactional/audit data, not solely in observability logs.
13. Azure workload identity is used for Azure resource access; end-user identity is used for business authorization.

## 2. Reference cloud components

### Public / synchronous

- Azure API Management Consumption as the initial, replaceable public API gateway.
- One transactional FastAPI deployment in Azure Container Apps Consumption.
- FastAPI is containerized.
- The API is a modular monolith: logical service boundaries are preserved inside one deployable.

### Asynchronous

- Azure Service Bus Standard.
- Separate Azure Container App workers with `min replicas = 0` for independently scaled asynchronous workloads.
- Scheduled finite work uses Azure Container Apps Jobs where appropriate.
- Durable event publication uses a transactional outbox.
- Service Bus consumers use durable inbox/message deduplication where applicable, in addition to domain/business idempotency.

### Persistence

- Azure Database for PostgreSQL Flexible Server.
- PostGIS enabled.
- PostgreSQL owns authoritative transactional state, idempotency/business constraints, inbox/outbox state, and durable operational history.
- Azure Blob Storage stores media and archived application logs.
- Blob lifecycle policies handle tiering/retention.

### Security

- Managed Identity for Azure-hosted workload identity wherever supported.
- Azure Key Vault for unavoidable external-provider secrets.
- Azure-resource calls use the workload's identity, not the end user's identity.

## 3. Domain modelling approach

Tirodhan should not invent novel data models for conventional application concerns.

Use established patterns for:

- account identity, verified contact credential, roles and sessions;
- multi-address customer profile;
- order/booking header and line items;
- logical payment, payment attempts, provider events and refunds;
- workforce/dispatch offers and assignments;
- per-stop/per-household fulfilment execution and attempt history;
- facility/receiving-point master data;
- evidence capture and object-storage attachment metadata.

The geographic planning/compaction workflow is the principal materially product-specific domain.

Detailed domain/schema authority is in:

- `docs/DOMAIN_MODEL.md`
- `docs/SCHEMA_DESIGN.md`
- `docs/ER_DIAGRAM.md`
- `docs/IDEMPOTENCY.md`

## 4. Transactional API modules

The single FastAPI deployment should preserve logical modules such as:

- identity/session;
- customer/address;
- serviceability;
- collection request;
- payment/refund;
- planning;
- rider/fleet;
- assignment;
- pickup execution;
- operations/escalation;
- receiving point/handover;
- evidence/media;
- reliability/outbox/inbox.

These are code/domain ownership boundaries, not separate MVP deployments.

## 5. Identity, roles and sessions

- All human login uses mobile number + OTP only.
- One human maps to one application user identity.
- A user may hold multiple application roles: `CUSTOMER`, `RIDER`, `MANAGER`.
- A newly verified user receives customer capability; rider/manager roles require explicit provisioning.
- A user has one active verified login mobile number at a time; changing the number preserves the same user identity and historical phone records.
- OTP verification is delegated to the OTP provider; Tirodhan never persists OTP values.
- After successful verification, Tirodhan issues its own short-lived access token and a revocable refresh-session mechanism.
- Refresh credentials are never stored in plaintext.
- Access tokens are not persisted as ordinary application data.
- The exact refresh-session retry/rotation/replay strategy is intentionally open. It must preserve revocation, safe replay handling, and a deliberate policy for the case where a refresh succeeds but the response is lost and the client retries the prior credential. An implementation agent must not choose this strategy implicitly.

MSG91 remains the current OTP-provider candidate; commercial terms/DLT onboarding must be confirmed before production commitment.

## 6. Address, serviceability and transaction snapshots

A customer may maintain multiple saved addresses.

Saved address/profile data is mutable master data. It is not authoritative for a historical booking after that booking is created.

Serviceability flow:

1. customer selects/confirms a saved or one-off address;
2. persist a short-lived serviceability context containing an immutable input snapshot;
3. asynchronous worker resolves/geocodes/validates serviceability and derives `cell_id`;
4. if checkout reaches the decision point before asynchronous resolution completes, the API runs the same domain operation synchronously;
5. serviceability must be confirmed before payment initiation.

The asynchronous and synchronous paths must invoke the same domain operation/invariants.

Accepted/paid collection requests preserve booking-time snapshots such as:

- address;
- spatial point;
- cell;
- slot;
- quoted price.

Later profile/address changes must not rewrite these facts.

## 7. Collection request and payment lifecycle

A durable `collection_request` is created before payment.

Core request states include:

- `PENDING_PAYMENT`
- `ACCEPTED`
- `PRE_PLANNING`
- `PLANNED`
- `CANCELLED`
- `COMPLETED`
- `EXPIRED`

Operational exception detail belongs to fulfilment/incident entities rather than exploding the request state machine.

### Pending payment

`PENDING_PAYMENT` has:

- configurable retry lifetime;
- transition to `EXPIRED` after that lifetime;
- separate later purge/reconciliation retention.

A frontend/provider-return timeout is not payment failure.

### Logical payment and attempts

Each CollectionRequest has one logical Payment obligation.

Gateway retries/interactions are PaymentAttempts:

```text
CollectionRequest
    └── Payment
          ├── PaymentAttempt 1
          ├── PaymentAttempt 2
          └── PaymentAttempt N
```

A failed PaymentAttempt does not make the logical Payment terminally failed while customer retry is still allowed.

Provider order/payment/event identifiers are persisted for deduplication and reconciliation.

Provider webhook and/or server-to-server reconciliation is authoritative; frontend return is UX only.

### Payment acceptance

A confirmed successful attempt, logical Payment success, request transition `PENDING_PAYMENT -> ACCEPTED`, and corresponding outbox event must be persisted atomically.

### Duplicate/late external success

More than one external attempt may theoretically succeed because of asynchronous provider races.

The system must record external truth rather than hiding it behind a uniqueness assumption.

Only one attempt satisfies the logical Tirodhan Payment. Any additional successful external charge becomes a reconciliation/refund condition and must never re-accept the request or duplicate downstream effects.

### Refunds

Refund is a separate financial lifecycle.

- refund against the actual successful provider charge/reference;
- payment instrument credentials are never stored;
- multiple/partial refunds may be represented even if MVP commonly uses full refunds;
- concurrent refunds must not exceed the successful payment amount;
- refund workers are idempotent;
- provider idempotency keys are used where supported;
- uncertain provider outcomes are reconciled rather than blindly retried.

## 8. Cancellation boundary

A customer may cancel while the request is `ACCEPTED`.

At the planning cutoff for the slot, eligible requests are frozen into an immutable planning batch and move to `PRE_PLANNING`.

Once `PRE_PLANNING`, customer cancellation is no longer automatically permitted.

Cancellation and planning freeze are competing atomic state transitions. Only one may win.

Where policy requires it, cancellation creates a Refund and a durable refund-requested event in the same transaction; provider execution may complete asynchronously.

## 9. System-wide idempotency and reliable events

Idempotency is an architectural invariant, not a worker-specific implementation detail.

Every state-changing operation that can be retried, replayed, redelivered, timed out, or executed concurrently must define:

- logical/business key;
- database/domain constraint;
- transaction boundary;
- replay result;
- concurrency rule;
- external-provider side-effect rule.

Use complementary layers:

```text
client/API command idempotency
        ↓
database/domain constraints
        ↓
inbox + transactional outbox
        ↓
provider idempotency / reconciliation
```

### API/mobile command idempotency

Where a retriable command uses an idempotency key:

- same scope/key + same request fingerprint returns/reconstructs the established result;
- same scope/key + different request fingerprint is rejected.

### Database/domain protection

Critical business invariants must be enforced through PostgreSQL transactions, unique/partial constraints, conditional transitions, row locking or equivalent DB mechanisms.

Do not rely on a Python read-then-write check to arbitrate critical races.

### Inbox

Transport-level message deduplication uses `(consumer, message_id)` or equivalent durable inbox semantics.

An inbox protects duplicate delivery of the same message. It does not replace business-key idempotency such as `planning_batch_id` or `refund_id`.

### Transactional outbox

Where a committed DB state change must cause an asynchronous event, the domain change and outbox event are committed in the same PostgreSQL transaction.

The outbox publisher may deliver more than once. Consumers remain idempotent.

The contract is:

> at-least-once transport + exactly-once intended business effect

not distributed exactly-once network execution.

See `docs/IDEMPOTENCY.md` and ADR-014.

## 10. Geographic planning

### Time boundary

Customers select 30-minute pickup slots.

A scheduled planning job runs a configurable `N` minutes before each slot. `N` is configuration, not an architectural constant.

### Space boundary

Requests are partitioned by a geographic `cell_id`.

Physical cell sizing is intentionally not yet chosen.

### Planning work unit

The work unit is an immutable planning batch for one cell and slot, not an individual request.

The scheduler discovers active cells for the upcoming slot and creates one logical planning batch/work item per cell/slot.

### Queue semantics

The geo-planning queue is serialized by cell. Service Bus sessions are the preferred mechanism for one active owner per cell while different cells may process concurrently.

Messages carry identifiers/control data, not complete request populations.

### Frozen population

At the planning boundary, selected `ACCEPTED` requests atomically receive the batch identity and move to `PRE_PLANNING`.

Late/new requests do not silently enter the existing immutable batch.

### Data access

The worker reads batch population from PostgreSQL with bounded/iterable access.

A selected algorithm may materialize a minimal spatial projection where required. The invariant is bounded memory and avoidance of unnecessary rich ORM materialization, not artificial request-by-request streaming.

PostGIS may perform candidate selection or clustering operations.

Phase 1G uses PostGIS geography predicates and meter distances to build one batch-scoped compatible-pair projection. The pure `BOUNDED_GREEDY_DIAMETER_V1` planner groups requests deterministically without receiving exact coordinates or rich request records.

### Attempts and broker deliveries

Logical compaction attempts are explicit business/operational attempts.

Service Bus DeliveryCount/redelivery is a different concept. A broker redelivery does not automatically consume another logical compaction attempt.

### Compaction outcome

Compaction produces collection groups:

- compatible neighbours -> compacted group;
- no compatible neighbour -> normal singleton group.

No valid request fails merely because it has no neighbour.

For `BOUNDED_GREEDY_DIAMETER_V1`, every pair in a compacted group must be within the batch's snapshotted compaction distance. Groups are bounded by the snapshotted maximum household-stop count. Weight, volume, item category, vehicle capacity, routing, and cross-cell compaction are not inputs to this algorithm. Cell technology/resolution remains open, so Phase 1G plans exactly one upstream cell batch at a time.

The algorithm version, distance, and maximum group-request count are immutable batch snapshots. All logical retries read these snapshots rather than current runtime configuration.

### Technical failure and fallback

The same immutable batch is retried after technical compaction failure.

After configurable maximum attempts `P`, the system deliberately produces fallback singleton groups and completes planning.

Infrastructure failure that prevents persistence is not solved by singleton fallback; the batch remains recoverable.

### Durable completion

The planning result is committed atomically:

- groups;
- membership;
- stable per-request PickupExecution records;
- requests `PRE_PLANNING -> PLANNED`;
- batch completion;
- outbox event(s).

A redelivery after durable completion has no additional business effect.

### Runtime AI

No LLM/generative-AI service is used in the runtime clustering path.

## 11. Rider, fleet and assignment

### Rider state

Separate rider intent from platform work state.

Availability intent, controlled by the rider:

- `OFFLINE`
- `AVAILABLE`

Current work state, controlled by platform operations:

- `IDLE`
- `RESERVED`
- `BUSY`

Assignment eligibility requires an approved/active rider plus appropriate intent, idle state, service-area/vehicle/capacity/slot eligibility.

Finishing an assignment returns work state toward `IDLE` without incorrectly changing the rider's chosen availability intent.

### Fleet affiliation

Fleet membership is historical affiliation, not rider identity.

### Assignment hierarchy

For each planned collection group:

1. use suitable available fleet capacity where possible;
2. otherwise fan out an offer to eligible independent riders;
3. first valid acceptance wins atomically;
4. if no acceptance arrives by the configured deadline, escalate to manager;
5. manager may manually assign/reassign.

Fleet, independent acceptance, and manual assignment all converge on the same durable RiderAssignment model.

### Assignment history

Assignment is not the household fulfilment object.

A stable PickupExecution exists per planned request. Rider assignments own PickupExecutions over time.

If a rider cannot continue:

- completed PickupExecutions remain completed;
- only outstanding PickupExecutions are released/reassigned;
- predecessor and successor assignments remain historical records.

At most one active assignment may own a collection group/work item according to the approved schema constraints.

## 12. Pickup execution and incidents

PickupExecution represents per-household fulfilment.

Attempt history and operational incidents are separate append/history entities.

Examples of incidents:

- customer unavailable;
- address/house not found;
- access blocked;
- rider unable to reach;
- rider unable to continue;
- other operational exception.

Possible human resolutions include retry, reassignment, cancellation, and cancellation+refund where applicable.

Completed pickup facts are immutable.

MVP human support:

- no custom in-app chat;
- dedicated WhatsApp Business support channel for active-pickup exceptions;
- prepopulate request/assignment context;
- WhatsApp is communication, not system of record;
- Operations records the resulting business action in Tirodhan.

## 13. Evidence and media

Evidence capture and media upload are separate concerns.

> Evidence capture is a business fact; media upload is a storage operation.

Evidence is represented as a capture/business event linked to pickup or handover. Physical files are MediaAssets stored in Blob Storage.

Requirements:

- media bytes do not belong in PostgreSQL;
- object names are opaque and contain no PII;
- client-generated capture/media identifiers support safe offline/mobile retry;
- direct client-to-Blob upload using short-lived authorization is preferred;
- upload/finalization is idempotent;
- media upload failure does not roll back a successfully performed pickup/handover;
- pending media remains app-private on the device and retries later;
- Blob lifecycle policy controls tiering/retention.

## 14. Receiving point and handover

Government/authorized kiosks/centres are represented as mutable `ReceivingPoint` master data.

A HandoverEvent is an immutable historical operational fact.

A handover preserves the validation basis used at the time, including relevant receiving-point location/geofence snapshots, so later master-data edits do not rewrite historical validity.

One HandoverEvent may cover several collected PickupExecutions.

A rejected/invalid handover attempt may be followed by a later valid handover; history is preserved.

Primary validation remains deterministic:

- registered receiving point;
- in-app capture;
- timestamp;
- observed device location/geofence;
- optional official identifier/QR/signage if available.

Do not depend on attaching a Tirodhan-owned QR to government property without permission.

AI vision is not a required validation dependency.

A CollectionRequest reaches `COMPLETED` after its collected material is associated with a valid receiving-point handover and required evidence is validly captured.

Blob upload may still complete asynchronously according to the evidence policy.

## 15. Workload identity and secrets

For Azure-hosted workloads:

- use Managed Identity for Azure-resource access wherever supported;
- use the executing workload identity, not the end user's identity;
- preserve end-user identity separately for business authorization/audit;
- use least privilege per workload;
- avoid one broad shared identity.

External-provider secrets that cannot use workload identity belong in Key Vault.

## 16. API ingress

Azure API Management Consumption is the initial public edge.

It may provide:

- routing;
- generic JWT validation;
- rate limiting/throttling;
- correlation/header policy;
- machine-authenticated backend invocation.

It must not own indispensable business logic.

FastAPI remains capable of enforcing application authorization independently.

APIM is replaceable. If gateway cost becomes material, clients may move to protected Container App ingress without redesigning business logic.

## 17. Network exposure

Initial intent:

- APIM: public;
- transactional API: publicly addressable as required by APIM Consumption topology, but strongly authenticated/authorized;
- workers/scheduled jobs: no public ingress;
- PostgreSQL/Service Bus/Key Vault: not public application surfaces;
- Blob: controlled direct upload/download only where explicitly authorized.

Private networking/endpoints should be evaluated against cost rather than added reflexively.

## 18. Observability

MVP intentionally avoids Application Insights, Log Analytics, and Azure Diagnostic Settings as the primary application-log pipeline.

### Application logs

- structured JSON;
- application writes to a shared replica-local volume;
- Fluent Bit sidecar tails/batches/compresses and uploads directly to Azure Blob;
- Blob access uses a narrowly scoped SAS for the dedicated log container;
- lifecycle policies control retention/tiering;
- no PII, exact coordinates, secrets, tokens, or payment-sensitive data in logs.

### Metrics/alerts

Use low-cost/native Azure platform metrics where useful.

Keep alerts small and actionable, such as:

- sustained API errors;
- Service Bus DLQ/non-processing;
- outbox backlog;
- repeated compaction technical failure;
- refund-processing/reconciliation failure;
- repeated container crash;
- database availability.

Business-critical history belongs in PostgreSQL, not only logs.

## 19. Cloud portability

Azure is the reference deployment platform, but application/domain code should avoid unnecessary Azure SDK semantics.

Use infrastructure/provider adapters where practical for:

- message bus;
- object store;
- secret provider;
- payment provider;
- OTP provider.

Do not create abstraction layers solely for theoretical purity.

## 20. Backup and recovery

### Recovery objectives

MVP targets:

- RPO: approximately 15 minutes or better for authoritative transactional data;
- RTO: approximately 2–4 hours for a major recovery event.

These targets do not justify active-active/high-availability regional architecture at MVP stage.

### PostgreSQL

Use Azure Database for PostgreSQL Flexible Server native point-in-time recovery.

Initial policy:

- 7-day PITR retention;
- no geo-redundant backup initially;
- no long-term retention initially;
- resource lock against accidental deletion where practical;
- periodic restore test.

A backup policy is incomplete unless restoration is tested.

### Blob

For media/evidence:

- blob soft delete;
- container soft delete;
- storage-account deletion protection where practical;
- lifecycle tiering/deletion;
- no blob versioning initially unless an actual overwrite-recovery need emerges.

Logs receive no separate backup policy beyond their Blob retention/lifecycle rules.

### Service Bus

Service Bus is not recovery storage.

Durable PostgreSQL state, inbox/outbox/domain state, and idempotent operations must allow required work to be recreated/re-driven where necessary.

## 21. Environment topology

Permanent environments:

- local;
- Azure `nonprod`;
- Azure `prod`.

Do not create permanent Azure dev/qa/staging by default.

Production-like staging is ephemeral and created from IaC only when required, then destroyed.

Production customer data is not copied directly to nonprod. Use synthetic/generated/anonymized test data.

## 22. Infrastructure as Code

Terraform is the selected IaC tool.

- Azure resources are provisioned through Terraform;
- nonprod and prod use separate state;
- ephemeral staging reuses the same modules/patterns;
- modules remove harmful duplication but do not become a large generic internal platform;
- recovery-critical configuration is reproducible from source control.

## 23. CI/CD

Provider intentionally remains open between:

- Azure DevOps Pipelines;
- GitHub Actions.

Provider-independent application flow:

1. lint/static checks;
2. unit tests;
3. relevant security checks;
4. build container image;
5. publish image;
6. deploy nonprod;
7. smoke/integration tests;
8. explicit production approval;
9. deploy prod.

Infrastructure flow:

1. Terraform fmt/validation;
2. Terraform plan;
3. human production review/approval;
4. Terraform apply.

Use workload federation/OIDC where supported instead of long-lived Azure deployment credentials.

## 24. Container registry

GitHub Container Registry (GHCR) is the default MVP registry.

Azure Container Registry Basic is a fallback only if a concrete Azure-specific authentication, reliability, operational, or deployment-integration problem makes GHCR materially inferior.

## 25. Still open

Do not silently decide:

- geographic cell resolution;
- clustering/compaction algorithm;
- detailed routing algorithm;
- item category taxonomy;
- final pricing formula;
- exact values for planning lead time `N`, compaction attempt limit `P`, rider acceptance deadlines, and retention periods;
- final payment gateway;
- final CI/CD provider;
- frontend/mobile technology;
- detailed material-mismatch workflow;
- final offline-evidence validation policy;
- exact refresh-session retry/rotation/replay semantics, including lost-success-response behaviour.
