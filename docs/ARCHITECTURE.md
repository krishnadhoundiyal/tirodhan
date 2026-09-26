# Architecture

## 1. Architectural principles

1. Azure is the reference MVP cloud, not a hard domain dependency.
2. Design logical services cleanly, but deploy as few units as economics justify.
3. Use scale-to-zero compute where appropriate.
4. PostgreSQL is the authoritative transactional state store.
5. Service Bus is a work/event transport, not a data store.
6. At-least-once delivery is expected; consumers must be idempotent.
7. Geographic compaction is deterministic optimization logic, not runtime generative AI.
8. Human operational fallbacks are first-class parts of fulfilment.
9. Business history belongs in transactional/audit data, not solely in observability logs.
10. Azure workload identity is used for Azure resource access; end-user identity is used for business authorization.

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

### Persistence

- Azure Database for PostgreSQL Flexible Server.
- PostGIS enabled.
- Azure Blob Storage for media and logs.
- Blob lifecycle policies handle tiering/retention.

### Security

- Managed Identity for Azure-hosted workload identity.
- Azure Key Vault for unavoidable external-provider secrets.
- Azure-resource calls use the workload's identity, not the end-user's identity.

## 3. Transactional API modules

The single FastAPI deployment should preserve logical modules such as:

- identity/session;
- customer;
- serviceability/address;
- collection request;
- payment/refund;
- planning;
- rider;
- assignment;
- pickup execution;
- operations/escalation;
- kiosk/handover;
- media.

These are code/domain ownership boundaries, not separate MVP deployments.

## 4. Customer identity and sessions

- Login method: mobile number + OTP only.
- OTP validation is delegated to an OTP provider; the application does not persist OTP values.
- After successful OTP verification, Tirodhan issues its own application access/refresh session.
- Access tokens are short lived.
- Refresh tokens are revocable and are never stored in plaintext.
- Authorization roles are application-owned: `CUSTOMER`, `RIDER`, `MANAGER`.
- A valid mobile number does not automatically confer rider/manager privileges.

MSG91 is the current OTP-provider candidate; commercial terms should be confirmed before production commitment.

## 5. Serviceability and cell precomputation

Serviceability/cell calculation begins as soon as the customer confirms a service address.

Preferred flow:

1. persist a serviceability context;
2. publish asynchronous work;
3. a worker resolves/validates the address as required, obtains/uses lat/long, derives `cell_id`, and stores the result;
4. if booking reaches the payment step before asynchronous resolution completes, the API performs the same serviceability operation synchronously;
5. the asynchronous path is an optimization and must never block booking.

The asynchronous and synchronous paths must invoke the same domain operation/invariants.

Serviceability must be known before payment is initiated.

## 6. Collection request and payment lifecycle

A durable `collection_request` is created before payment.

Core states:

- `PENDING_PAYMENT`
- `ACCEPTED`
- `PRE_PLANNING`
- `PLANNED`
- later execution/completion states as appropriate
- `CANCELLED`
- `EXPIRED`

### Pending payment

`PENDING_PAYMENT` rows have:

- a configurable retry lifetime (`payment_expires_at`);
- an expiry transition after the retry lifetime;
- a separate later purge/retention boundary.

A UI/payment timeout is not equivalent to payment failure.

### Payment initiation

- payment initiation occurs synchronously as part of the customer flow;
- the customer may be redirected to the payment provider UI;
- browser/app return is UX, not authoritative payment proof;
- provider webhook and/or server-to-server provider reconciliation is authoritative.

### Acceptance

Successful payment moves the collection request to `ACCEPTED` and confirms the selected collection slot to the customer.

Notification of acceptance is asynchronous.

### Refunds

Refunds are separate entities/processes.

- store provider transaction identifiers needed to refund the original successful debit;
- never store card/CVV/UPI PIN/bank credentials;
- refund against the original provider payment reference;
- provider is responsible for crediting the original payment method;
- refund state is independent from request state;
- refund processing must be idempotent.

## 7. Cancellation boundary

A customer may cancel while the request is still `ACCEPTED`.

At the planning cutoff for the slot, eligible requests are frozen into an immutable planning batch and move to `PRE_PLANNING`.

Once a request is `PRE_PLANNING`, customer cancellation is no longer automatically permitted.

Cancellation and planning-freeze must be implemented with an atomic/conditional state transition so only one wins a race.

Where policy requires it, cancellation initiates a refund against the original payment.

## 8. Geographic planning

### Time boundary

Customers select 30-minute pickup slots.

A scheduled planning job runs a configurable `N` minutes before each slot. `N` is configuration, not an architectural constant.

### Space boundary

Requests are partitioned by a geographic `cell_id`.

Physical cell sizing is intentionally not yet chosen.

### Planning work unit

The distributed work unit is not an individual request.

It is an immutable planning batch for a cell and slot, conceptually:

`(planning_batch_id, cell_id, slot_id)`

The scheduled job discovers active cells for the upcoming slot and creates one planning batch/work item per active cell.

### Queue semantics

The geo-planning queue is logically partitioned/serialized by cell. Service Bus sessions are the preferred mechanism so one cell has a single active compaction owner while different cells may process concurrently.

Queue messages should carry identifiers/control-plane data, not the entire request dataset.

### Data access

The worker owns one `(cell, slot, batch)` exclusively.

It reads the batch's requests from PostgreSQL using bounded/iterable access.

Do not require full ORM entities to be loaded merely for clustering.

The clustering implementation may materialize a minimal spatial working set if the selected algorithm requires all coordinates. Bounded access is the invariant; request-by-request streaming is not a requirement if it damages the algorithm.

PostGIS may perform part or all of spatial candidate selection/clustering.

### Compaction outcome

The compactor's job is to identify neighbours/groups.

There is no business-level "failed request" caused by inability to find a neighbour.

- requests that cluster become grouped collection units;
- requests that do not cluster become singleton collection units.

All valid requests entering `PRE_PLANNING` must eventually become `PLANNED`.

### Technical failure and retry

Service Bus uses Peek-Lock / at-least-once delivery.

The planning batch is the authoritative idempotency boundary.

- a technical crash leaves requests in `PRE_PLANNING`;
- the same immutable batch is retried;
- workers must safely handle duplicate/redelivered messages;
- a message is settled only after the durable planning result is committed;
- duplicate processing must never produce duplicate planning results.

The maximum compaction attempt count is configurable (`P`); no fixed number is frozen yet.

After the configured compaction attempts are exhausted, the system degrades to singleton planning:

`one request = one collection unit`

The batch is then persisted as a valid `PLANNED` result.

Infrastructure failure that prevents persistence is not solved by singleton fallback; it remains a recoverable operational failure.

### Runtime AI

Do not use an LLM/generative-AI service in the runtime compaction path.

AI may assist engineering/testing/tuning, but production clustering is deterministic spatial/optimization logic.

## 9. Rider assignment

The assignment unit is a planned collection group, not an individual request unless the group is a singleton.

### Rider availability

Rider operational state is application-owned.

At minimum:

- `OFFLINE`
- `AVAILABLE`
- assigned/busy states as implementation requires.

The rider controls `AVAILABLE`/`OFFLINE`.

Only `AVAILABLE` and otherwise eligible riders may receive offers.

Availability and eligibility are separate concepts.

### Assignment hierarchy

For each planned collection group:

1. check whether an enabled fleet has available capacity for the cell + slot;
2. if suitable fleet capacity exists, assign from that fleet;
3. otherwise fan out the work opportunity to eligible `AVAILABLE` independent riders;
4. first valid acceptance wins atomically;
5. if nobody accepts by the configured deadline, escalate to a manager;
6. the manager manually chooses/assigns a rider; the choice itself is outside software optimization.

Manual assignment must not allow duplicate assignment of the same collection group.

## 10. Pickup execution and partial progress

Execution state must exist per household pickup, not only at collection-group level.

If a rider has a group of `X` pickups and completes `Y` before becoming unable to continue:

- the `Y` completed pickups remain completed;
- only the `X-Y` outstanding pickups are reassigned;
- historical assignment state is retained rather than overwritten;
- a new assignment may be created for residual work.

Completed operational facts are immutable.

## 11. Reachability / no-show exceptions

Examples include:

- house not found;
- customer unavailable;
- access blocked;
- rider unable to reach;
- rider unable to continue;
- other operational exception.

These are exception reasons on pickup execution, not necessarily distinct collection-request states.

Flow:

1. rider/customer raises an exception;
2. human operations is notified;
3. operations may facilitate location/contact;
4. outcome may be retry, reassignment, cancellation, and where appropriate refund.

MVP support channel:

- no custom in-app chat;
- use a dedicated WhatsApp Business support channel for active-pickup exceptions;
- prepopulate request/assignment context in the message;
- WhatsApp is the communication medium, not the system of record;
- the manager records the resulting business action in Tirodhan.

## 12. Media evidence

Collection and handover evidence is stored in object storage.

- media is tagged/associated with request and pickup-execution identifiers;
- do not store image/video bytes in PostgreSQL;
- media upload is retryable;
- upload failure must not roll back a successfully performed pickup;
- storage lifecycle policies, not application cron code, move old media to colder tiers/delete according to retention policy.

Direct client-to-Blob upload using short-lived authorization is preferred over proxying large media through FastAPI.

## 13. Kiosk / receiving-point completion

Government/authorized kiosks are represented internally with identifiers and known locations.

MVP completion evidence:

- evidence must be captured in-app;
- the capture must correspond to a registered kiosk/receiving point;
- geofence/location validation is used;
- an official/approved kiosk QR/signage identifier may later be added as an additional factor;
- the architecture must not depend on attaching a Tirodhan-owned QR to government property without permission.

A request becomes complete when the material has been deposited/handed over at the designated receiving point and required evidence has been validly captured.

Blob upload may complete asynchronously.

## 14. Workload identity and secrets

For Azure-hosted workloads:

- use Managed Identity for Azure-resource access wherever supported;
- use the workload identity of the executing service, not the end user's identity;
- preserve end-user identity separately for business authorization/audit;
- use least privilege per workload;
- avoid one broad shared identity for every component.

External-provider secrets that cannot use workload identity belong in Key Vault.

## 15. API ingress

Azure API Management Consumption is the initial public edge.

It may provide:

- routing;
- generic JWT validation;
- rate limiting/throttling;
- correlation/header policy;
- machine-authenticated backend invocation.

It must not own indispensable business logic.

FastAPI must remain capable of enforcing application authorization independently.

APIM is explicitly replaceable. If gateway cost becomes material, clients may be moved to the Container App ingress without redesigning business logic.

## 16. Network exposure

Initial intent:

- APIM: public;
- transactional API: publicly addressable as required by APIM Consumption topology, but strongly authenticated/authorized;
- workers/scheduled jobs: no public ingress;
- PostgreSQL/Service Bus/Key Vault: not public application surfaces;
- Blob: controlled direct upload/download only where explicitly authorized.

Private networking/endpoints should be evaluated against cost rather than added reflexively.

## 17. Observability

MVP intentionally avoids Application Insights, Log Analytics, and Azure Diagnostic Settings as the primary application-log pipeline.

### Application logs

- structured JSON;
- application writes to a shared replica-local volume;
- Fluent Bit runs as a sidecar;
- Fluent Bit tails/batches/compresses and uploads logs directly to Azure Blob Storage;
- Blob access uses a narrowly scoped SAS for the log container;
- lifecycle policies control log retention/tiering;
- no PII/secrets/tokens/payment-sensitive data in logs.

### Metrics

Use low-cost/native Azure platform metrics where useful.

Keep alerts small and actionable, e.g.:

- sustained API errors;
- Service Bus DLQ/non-processing;
- repeated compaction technical failure;
- refund-processing failure;
- repeated container crash;
- database availability.

Business-critical history must be stored in PostgreSQL/audit data, not only logs.

## 18. Cloud portability

Azure is the reference deployment platform, but domain/application code should depend on abstractions around infrastructure concerns where practical, e.g.:

- message bus;
- object store;
- secret provider;
- payment provider;
- OTP provider.

Do not build artificial abstraction layers where they add no value, but do not spread Azure SDK semantics through business logic.

## 19. Backup and recovery

### Recovery objectives

MVP targets:

- **RPO:** approximately 15 minutes or better for authoritative transactional data;
- **RTO:** approximately 2–4 hours for a major recovery event.

These targets do not justify active-active or high-availability regional architecture at MVP stage.

### PostgreSQL

Use Azure Database for PostgreSQL Flexible Server native backup / point-in-time restore capabilities.

Initial policy:

- 7-day PITR retention;
- no geo-redundant backup initially;
- no long-term retention initially;
- protect the database/server resource against accidental deletion with an Azure resource lock where practical;
- periodically perform and validate a restore test.

A restore test is part of the recovery policy. Backup existence without tested restoration is insufficient.

### Blob Storage

For media/evidence storage:

- enable blob soft delete;
- enable container soft delete;
- protect the storage account against accidental deletion where practical;
- retain normal lifecycle policies for tiering/deletion;
- do not enable blob versioning initially unless an actual overwrite/recovery requirement emerges.

Logs are not authoritative business state and do not receive a separate backup policy beyond their configured Blob retention/lifecycle policy.

### Service Bus

Service Bus is not a backup system.

Authoritative business state remains in PostgreSQL. If queue state is lost or delayed, durable database state must allow operational work to be recreated/re-driven where required.

## 20. Environment topology

MVP uses:

- local developer environment;
- one shared Azure `nonprod` environment;
- one Azure `prod` environment.

Do not create permanent `dev`, `qa`, and `staging` Azure environments by default.

If production-like staging is required for a release or infrastructure change, create it ephemerally from Infrastructure-as-Code and destroy it afterwards.

Production customer data must not be copied directly into non-production environments. Use generated, synthetic, or appropriately anonymized test data.

## 21. Infrastructure as Code

Terraform is the selected Infrastructure-as-Code tool.

Requirements:

- Azure resources are provisioned through Terraform;
- `nonprod` and `prod` use separate Terraform state;
- ephemeral staging is created from the same reusable modules/configuration patterns;
- modules should remove harmful duplication but must not become a large generic internal platform;
- infrastructure configuration required for disaster recovery should be reproducible from source control.

## 22. CI/CD

The CI/CD provider is intentionally not yet fixed.

Approved candidates:

- Azure DevOps Pipelines;
- GitHub Actions.

The workflow contract is provider-independent.

### Application pipeline

Expected flow:

1. lint/static checks;
2. unit tests;
3. relevant security checks;
4. build container image;
5. publish image;
6. deploy to `nonprod`;
7. run smoke/integration tests;
8. require explicit production approval;
9. deploy to `prod`.

### Infrastructure pipeline

Expected flow:

1. `terraform fmt` / validation;
2. Terraform plan;
3. human review/approval for production;
4. Terraform apply.

Use workload federation / OIDC where supported rather than long-lived Azure deployment credentials.

Production deployment should not occur automatically merely because a change is merged.

## 23. Container registry

GitHub Container Registry (GHCR) is the default MVP container registry.

Azure Container Registry (ACR) Basic is the fallback if a concrete Azure-specific authentication, reliability, operational, or deployment-integration problem makes GHCR materially inferior.

Do not introduce ACR merely for architectural neatness when GHCR is operating satisfactorily.

## 24. Still open

Do not silently decide:

- geographic cell resolution;
- clustering/compaction algorithm;
- detailed routing algorithm;
- exact values for `N`, `P`, acceptance deadlines, and retention periods;
- final payment gateway;
- final CI/CD provider (Azure DevOps Pipelines vs GitHub Actions);
- frontend technology;
- detailed mismatch workflow when presented material differs from booking.
