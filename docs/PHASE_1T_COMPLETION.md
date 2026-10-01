# Phase 1T completion and review notes

Branch: `phase1/serviceability-runtime`. Exact base:
`58c3e57f502de0caf4cff667308a3ac51ed4b20c`.
Architecture freeze: `bc39ab79d63ff9e395077ce754f4f30674b3bf57`.
Implementation is committed separately; see the accompanying delivery message for its SHA.
No merge or Azure deployment was performed.

## Verification

- Previously interrupted worker/checkout collision test: 1 passed.
- Adapter/runtime unit suite: 45 passed.
- Focused PostgreSQL serviceability, checkout/payment and identity regressions: 64 passed.
- Final combined focused run: 109 passed (45 unit + 64 integration/regression).
- Full `pytest -q`, with disposable PostgreSQL/PostGIS integration enabled: 460 passed,
  0 skipped (202.03 seconds).
- Ruff check and format check: passed.
- mypy: passed, 94 source files.
- `git diff --check`: passed.
- Alembic current and heads: `0015_authentication_challenge`; no migration introduced.
- Terraform formatting: passed. Provider validation was not completed: the interrupted
  run encountered registry/network failure and subsequently stalled during provider
  validation. No successful validate, plan or apply is claimed. Temporary provider/cache
  files remain ignored; the generated validation-only module lock file was removed.

## Functional architecture check

After rereading AGENTS, ARCHITECTURE, IDEMPOTENCY and ADR-001/002/012/014, the implementation
retains asynchronous primary resolution plus synchronous checkout fallback, using one
shared `resolve_serviceability` operation. PostgreSQL remains authoritative through a
conditional PENDING-to-terminal update. Google and broker calls occur outside DB sessions
and transactions. H3 resolution is exactly 7. No Redis, distributed locks, global mutex,
extra schema or replacement reliability store was introduced.

`GoogleGeocodingLocationResolver` in
`src/tirodhan/modules/serviceability/google_geocoding.py` uses stable v3 REST/httpx,
structured IN/Delhi validation, conservative forward precision and exact supplied-pin
preservation after reverse validation. Configuration failures fail closed; technical
provider failures use controlled codes. Application-owned clients are reused and closed;
injected clients remain caller-owned. HTTP automatic retries are explicitly disabled.

`H3CellIdDeriver` in `src/tirodhan/modules/serviceability/h3_cells.py` persists raw canonical
H3 strings at the code constant resolution 7, independently of Google.

## Authentication safety

The access-principal dependency now owns a short read session instead of retaining a
request-wide auth transaction. The existing JWT validation and live PostgreSQL refresh
session/user/role checks are unchanged. It returns a frozen principal containing UUIDs
and a frozenset of role strings, not detached ORM objects. Every request reads anew.

The prior authorization reads did not lock rows; retaining their transaction through
the handler never prevented a concurrent revocation. Closing that unlocked read introduces
no new lock-release race and preserves the approved point-in-time/next-request semantics.
Fresh operational assignment eligibility locks, refresh/logout locks and login transactions
are untouched. Tests reject the next checkout after disablement, session revocation/expiry,
role revocation and invalid/expired JWTs before Google or pricing. A real-JWT checkout test
checks PostgreSQL activity during provider invocation, including the separate API engine,
to prove no auth transaction survives into the call.

## Messaging, transactions and crashes

Topology: one dedicated configurable serviceability queue in Service Bus Standard; no
sessions required for this responsibility. The separate ACA consumer has no ingress,
min replicas 0, managed-identity scaling, Peek-Lock and bounded lock renewal. Workload
identities receive queue-scoped Data Sender/Data Receiver permissions, not manage rights.
The API does not publish directly and requires no broker permission.

The finite bounded publisher entry point is `python -m tirodhan.workers.outbox_publisher`.
The scheduled ACA Job deployment mechanism is provisional pending deployment review.
It explicitly routes only ServiceabilityRequested; unrelated outbox types remain pending.
Publish-attempt metadata commits before send; PUBLISHED commits only after successful send.
Send failure leaves the row recoverable. A crash between send and mark resends the same
outbox UUID transport identity. Overlapping executions may send duplicates safely.

Consumer entry point: `python -m tirodhan.workers.serviceability`.
Consumer name: `serviceability-resolver`. Inbox transport key: `(consumer_name, message_id)`;
business key: serviceability-context UUID. PROCESSING commits before resolution; PROCESSED
commits before broker completion. PROCESSING redelivery resumes, terminal replay avoids
Google, and PROCESSED skips work. Broker delivery count is not a business attempt counter.
Tests cover send-before-mark failure, duplicate concurrent sends/deliveries, terminal-commit
crash before inbox completion, and inbox-commit crash before settlement.

Worker and checkout may both call Google concurrently. Their conditional update selects
one terminal winner; the loser returns that persisted result. Tests include conflicting
outcomes where a worker's UNSERVICEABLE result wins and checkout performs no pricing.
Completed booking replay and terminal contexts invoke no new Google call; booking replay
also invokes no pricing. Final booking transaction revalidates the context.

Messages contain only the internal context UUID, stable message UUID and controlled type;
no address, coordinates, key or provider body enters outbox/inbox/broker metadata. Google
responses/descriptors are not stored wholesale. HTTP request diagnostics are suppressed
even at application DEBUG; file-log tests prove sensitive synthetic inputs and URLs are
absent. Worker/publisher error handling never logs raw SDK/provider exceptions.

## Runtime configuration

All new settings use the `TIRODHAN_` prefix:

- `GOOGLE_MAPS_API_KEY` (secret), `GOOGLE_MAPS_HTTP_TIMEOUT_SECONDS`,
  `GOOGLE_MAPS_DELHI_ADMIN_ALIASES`;
- `SERVICE_BUS_NAMESPACE` (fully qualified), `SERVICEABILITY_QUEUE_NAME`,
  `SERVICE_BUS_MANAGED_IDENTITY_CLIENT_ID`, `SERVICE_BUS_OPERATION_TIMEOUT_SECONDS`;
- `SERVICEABILITY_LOCK_RENEWAL_SECONDS`, `OUTBOX_PUBLISH_BATCH_SIZE`.

Existing address-encryption keyring settings are required by the worker and API fallback.
Missing Google settings leave the resolver unconfigured; there are no dummy locations/cells.
Non-Azure secrets arrive through Key Vault/ACA references, without application Key Vault calls.

## Changed files grouped by concern

Paths below are repository-relative and cover both Phase 1T commits.

- Architecture/docs: `.env.example`, `README.md`, `docs/PROJECT_CONTEXT.md`,
  `docs/ARCHITECTURE.md`, `docs/DOMAIN_MODEL.md`, `docs/SCHEMA_DESIGN.md`,
  `docs/IDEMPOTENCY.md`, `docs/DATA_PROTECTION.md`,
  `docs/adr/ADR-004-planning-compaction.md`,
  `docs/adr/ADR-015-serviceability-location-runtime.md`, this report.
- Google/H3: `src/tirodhan/modules/serviceability/google_geocoding.py`,
  `src/tirodhan/modules/serviceability/h3_cells.py`.
- Shared domain boundary: `src/tirodhan/modules/serviceability/service.py`.
- Checkout/auth boundary: `src/tirodhan/modules/collection_requests/service.py`,
  `src/tirodhan/api/routes/collection_requests.py`, `src/tirodhan/api/dependencies.py`.
- Outbox/broker adapters: `src/tirodhan/modules/reliability/publisher.py`,
  `src/tirodhan/modules/reliability/service_bus.py`.
- Consumer/processes: `src/tirodhan/modules/serviceability/consumer.py`,
  `src/tirodhan/workers/__init__.py`, `src/tirodhan/workers/serviceability.py`,
  `src/tirodhan/workers/outbox_publisher.py`.
- Runtime/dependencies: `src/tirodhan/modules/serviceability/runtime.py`,
  `src/tirodhan/main.py`, `src/tirodhan/core/config.py`,
  `src/tirodhan/core/logging.py`, `pyproject.toml`.
- Terraform: `.gitignore`, `infra/terraform/README.md`,
  `infra/terraform/serviceability/{versions,variables,main,outputs}.tf`.
  The focused module creates/reuses Standard namespace, dedicated queue, worker/scaler,
  provisional publisher Job, separate identities, entity-scoped broker RBAC and
  individual-secret-scoped Key Vault grants/references. Shared estate is supplied as inputs.
- Tests: `tests/unit/test_serviceability_adapters.py`,
  `tests/integration/test_serviceability_runtime.py`,
  `tests/integration/test_customer_serviceability.py`,
  `tests/integration/test_collection_payment.py`.

## Remaining operational review

No unresolved functional architecture decision was silently chosen. Hosted rollout still
requires provider validation/plan review, existing-estate inputs and network/image access,
reviewed schedule/timeouts/capacity, Google key restrictions/billing and secret provisioning.
The existing ADR-009 shared-volume/Fluent Bit logging integration remains host-estate work;
this phase does not choose a volume path or expand the logging infrastructure. There were
no live Google/Service Bus calls or infrastructure deployments in verification.
