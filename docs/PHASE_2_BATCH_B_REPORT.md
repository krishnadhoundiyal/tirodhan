# Phase 2 / Batch B financial and cancellation implementation report

Implementation date: 2026-10-09. Local architectural review only. The canonical-charge customer
flow is implemented; the unrestricted production definition of done remains blocked by the
separate additional-charge accounting and historical-recovery decisions in item 19. No capability
is enabled merely because a mocked regression passed.

## 1. Working directory

Backend: `C:\Users\91956\Tirodhan\tirodhan`. Frontend inspected read-only at
`C:\Users\91956\Tirodhan-frontend\tirodhan-frontend`.

## 2. Base main SHA and preflight

Clean backend `main`, matching the local `origin/main` reference, was
`3397c06b74b8561a1a429b759a2e11d83ad6e0a6` (merge PR #48). Batch A commit
`2634ecb62294ce1202925da37db006cca7bd83b0` and provider commit
`2eeff59c93d342a780dc4cd57e188b5ed646c244` were verified ancestors. Existing checkout
endpoint was present. No stale-base cherry-pick was used.

Repository instructions, project context, architecture, domain, schema/ER, privacy, idempotency
and relevant payment/planning/reliability ADRs were reviewed. Frontend contracts, clients, errors,
gates and financial/cancellation callers were inspected. Its clean `main` SHA is
`103d181eae4ebc539f8253cb009ac1b42c953e12`.

## 3. Branch

`phase2/customer-financial-cancellation`, created from the verified main above.

## 4. Commit SHA

The exact local commit SHA is supplied in the delivery message. Recording this report's own SHA
inside itself would be self-referential. No push, PR, merge or deployment is authorized/performed.

## 5. Working tree

Post-commit cleanliness is verified and supplied in the delivery message. Test artifacts reside
in ignored `.pytest_cache`; no credentials or test environment file is committed.

## 6. Exact changed files

```text
docs/DOMAIN_MODEL.md
docs/ER_DIAGRAM.md
docs/IDEMPOTENCY.md
docs/PHASE_2_BATCH_B_REPORT.md
docs/SCHEMA_DESIGN.md
docs/adr/ADR-005-payments-refunds.md
migrations/versions/0019_customer_financial_events.py
src/tirodhan/api/router.py
src/tirodhan/api/routes/collection_requests.py
src/tirodhan/api/routes/customer_finance.py
src/tirodhan/api/routes/payments.py
src/tirodhan/modules/collection_requests/cancellation.py
src/tirodhan/modules/collection_requests/expiry.py
src/tirodhan/modules/customer_reads/finance.py
src/tirodhan/modules/customer_reads/projections.py
src/tirodhan/modules/customer_reads/repository.py
src/tirodhan/modules/payments/models.py
src/tirodhan/modules/payments/refunds.py
src/tirodhan/modules/payments/service.py
tests/integration/test_cancellation_refunds.py
tests/integration/test_collection_payment.py
tests/integration/test_customer_financial_cancellation.py
tests/integration/test_customer_financial_migration.py
tests/integration/test_customer_mobile_core.py
tests/integration/test_razorpay_payments.py
tests/unit/test_customer_read_projections.py
```

## 7. API methods and paths

New: `GET /v1/customer/collection-requests/{request_id}/payment` and
`GET /v1/customer/collection-requests/{request_id}/refunds`.

Extended existing bodyless command: `POST /v1/collection-requests/{request_id}/cancel`, retaining
`Idempotency-Key` and its existing `CollectionRequestResponse`.

Preserved existing `GET /v1/customer/payment-attempts/{payment_attempt_id}/checkout`,
`POST /v1/payments/collection-requests/{request_id}/attempts` and
`POST /v1/payments/provider/webhook`. No direct customer refund mutation or journey endpoint.

## 8. Frontend contract matrix

| Contract | Backend match and authority |
|---|---|
| Payment read | Exact path and direct PaymentDto, no wrapper. `payment_id`, `request_id`, integer `amount_minor`, `currency`, `status`, `retry_allowed`, nullable `expires_at`, `succeeded_at`, `current_attempt`. Attempt has only UUID/status. |
| Payment status | `PENDING`, `PROCESSING`, `CONFIRMING`, `FAILED`, `SUCCEEDED`, `CANCELLED`, `EXPIRED`. CREATED -> PROCESSING; INITIATION_UNCERTAIN -> CONFIRMING. Canonical success survives additional-charge reconciliation. No client success authority. |
| Attempt status | `PENDING`, `PROCESSING`, `SUCCEEDED`, `FAILED`, `CONFIRMING`; existing projection reused. Latest chronological attempt retained, including failed/uncertain history. |
| Retry | Requires unsettled collection/Payment, live expiry, all attempts definitively FAILED (or none), no pending reconciliation, no cutoff/frozen work unit. Mutation enforces the unsettled/reconciliation guard under Payment lock. Exact attempt replay remains recoverable. |
| Refund read | Exact path and `{refunds: RefundDto[]}`. Real rows only, ordered by initiation time/UUID; UUID, status, integer amount, currency, initiated/completed timestamps and public reason. |
| Refund status | PENDING -> INITIATED; PROCESSING/SUBMITTED -> PROCESSING; INITIATION_UNCERTAIN -> CONFIRMING; SUCCEEDED with confirmation timestamp -> COMPLETED; FAILED -> FAILED. Cancellation never means COMPLETED. |
| Refund reason | CUSTOMER_CANCELLATION preserved. Controlled ADDITIONAL_SUCCESS/LATE_SUCCESS mean financial correction and map narrowly to PAYMENT_CORRECTION. Existing SERVICE_UNAVAILABLE/PAYMENT_CORRECTION public values remain readable. OPERATIONS_ADJUSTMENT has no approved meaning in this enum: safe 503, never a fabricated correction. |
| Eligibility | Existing embedded allowed/cutoff_at/reason/refund_expectation shape. Confirmed full compensation -> FULL_PAYMENT; no settled funds -> NONE; unapproved partial balance -> REVIEW_REQUIRED and refusal. Unsettled pre-cutoff cancellation supports the requested cancellation-before-capture race. |
| Mutation conflicts | Existing successful response unchanged. Authoritative cutoff/freeze/financial conflicts return `{error:{code}}` with PLANNING_CUTOFF_REACHED, PLANNING_STARTED or NOT_ELIGIBLE. Missing planning configuration is safe 503. Existing missing-key/ownership/idempotency behavior retained. |
| Authentication/isolation | Live CUSTOMER session/user/roles required. Read endpoints conceal absent and foreign collections with 404; missing/invalid session 401, missing role 403, malformed UUID 422. Provider secrets/charge IDs are absent from public DTOs. |
| Read cache/snapshot | Both GETs use existing private, no-store, REPEATABLE READ / READ ONLY dependency. Shared finance loader/projections also serve embedded detail/list financial facts. |
| Capability gates | Frontend untouched. payment/refunds/checkout/cancellationCompensation remain subject to explicit frontend configuration, nonprod validation and item 19 decisions. |

## 9. Cancellation/refund transaction

1. Existing live customer dependency authorizes the caller; persisted request verifies ownership.
2. Claim existing cancellation scope/key/fingerprint. Exact completed replay reloads the committed
   resource, preventing a stale ACCEPTED object after an idempotency-key wait.
3. Lock Payment, then cell/slot advisory lock, then reload request FOR UPDATE. Revalidate work-unit
   identity, cutoff, persisted batch, status and quote/payment accounting.
4. For confirmed canonical funds, inspect actual refund reservations. Zero reserved balance creates
   a full CUSTOMER_CANCELLATION Refund through existing create_refund. Full reservation reuses its
   obligation; partial/unrepresentable balance refuses before cancellation.
5. Refund creation validates actual canonical succeeded Attempt and charge reference, claims the
   stable refund business key, inserts Refund and identifier-only RefundRequested outbox, and
   completes refund-command idempotency.
6. Persist request CANCELLED/cancelled_at; close an unsettled Payment as CANCELLED without a refund.
   Complete cancellation command in this same transaction and commit. Any failure rolls back all
   effects. Existing worker performs provider execution after commit.

## 10. Lock acquisition before/after

| Operation | Before | After |
|---|---|---|
| Capture/reconciliation | Provider-event identity -> Attempt -> Payment -> work-unit | Provider-event identity -> sorted Payments -> sorted Attempts -> work-unit advisory -> request -> compensation refund command/Refund/outbox |
| Cancellation | Cancellation key -> work-unit -> request | Cancellation key -> Payment -> work-unit -> request -> refund key/Refund/outbox |
| Refund intent | Refund key -> Payment | Payment -> refund key -> canonical Attempt read -> Refund/FK checks -> outbox |
| Attempt initiation | Attempt key -> Payment -> new Attempt | Same; guard new keys while pending/uncertain/reconciling; HTTP outside DB; result locks only Attempt |
| Expiry | Payment/request per unsorted candidate | Candidate Payment UUID order -> Payment/request per candidate |
| Freeze | Work-unit advisory -> unique batch -> conditional request updates | Unchanged; never requests financial ancestor locks |
| Refund worker | Separate inbox/Refund claim/result/completion transactions, HTTP between | Unchanged; no held DB transaction during HTTP, native refund identity reused |
| Refund webhook | Provider-event identity -> sorted Refund rows; plain financial-reference reads | Unchanged; never takes Payment/work-unit locks after Refund |
| Financial reads | Read-only coherent snapshot | Same; no locks/provider side effects |

Payment precedes locked Attempt/FK checks and work-unit/request in overlapping financial commands.
No work-unit/request owner subsequently asks for Payment. Refund workers/webhooks do not obtain
ancestor locks after Refund. Distinct command scopes do not share keys; refund key now follows
Payment to avoid a key/Payment cycle. Conflicting event identities lock candidate Payments/Attempts
in UUID order. A mapping newly visible after lookup rolls back for redelivery rather than reversing
order. Locks serialize only involved payments/cell-slots. See IDEMPOTENCY.md for the full boundary.

## 11. PostgreSQL concurrency tests

Real PostgreSQL transactions test same-key and different-key cancellation, capture/cancellation
with both controlled winners, and freeze/cancellation with both controlled winners. Tests hold
winner locks with barriers; waiting cancellation is observed in pg_stat_activity. Other directions
assert blocked tasks before release, use bounded waits and cancellation lock timeout, and inspect
committed Payment/request/Refund/outbox truth. New attempt keys race under Payment and yield one
attempt plus one refusal; a definitively failed attempt can retry. Refund worker HTTP overlaps under
different transport UUIDs and converges through one native provider key/body/effect.

## 12. Capture/cancellation race

Capture wins: one canonical acceptance/outbox, followed by CANCELLED and one full Refund/outbox.
Cancellation wins: no acceptance, Payment closed, then verified capture establishes canonical
financial success and full compensation atomically, leaving request CANCELLED. Duplicate/reordered
capture does not add another refund or acceptance. A freeze winner refuses cancellation and creates
no compensation; a cancellation winner is excluded from the frozen accepted population.

## 13. Atomicity and crashes

Injected failures after RefundRequested persistence and after cancellation-command completion leave
no committed cancellation/refund/outbox/command. Retry converges to one intent/event. Failure after
late-capture compensation outbox rolls back provider event, succeeded Attempt/Payment and refund;
redelivery recovers atomically. Missing compensation retention also rolls back capture instead of
acknowledging an unrecorded obligation. Post-commit same/different cancellation-key replays and
duplicate capture return/reuse durable truth.

## 14. Idempotency and balance

Existing cancellation scope/fingerprint and changed-fingerprint conflict behavior retained. Stable
refund key `customer-cancellation:<request_id>` in `refund.create:<payment_id>` is independent of
transport and cancellation IDs. Existing unique provider/refund identities and outbox event key
protect duplicates. Reserved PENDING/PROCESSING/SUBMITTED/SUCCEEDED/INITIATION_UNCERTAIN sums are
checked under Payment lock against its integer amount; FAILED does not reserve. Canonical settled
Attempt/reference and matching currency are required. Partial reservation refuses fresh
cancellation; already fully reserved compensation adds no intent. Provider-neutral `refund:<uuid>`
and native `rf_<uuid_hex>` keys/bodies remain unchanged.

Worker tests cover normal execution, redelivery, concurrent transport IDs and remote effect followed
by a lost response. Uncertainty remains durable, and replay uses one native effect. These are mocked
HTTP tests, not Razorpay sandbox verification.

## 15. Late-payment reconciliation

A cancelled request's first canonical capture compensates even after expiry/freeze or an uncertain
attempt. Financial success is recorded without collection acceptance; refund execution remains
independent. Additional distinct charges are recorded as RECONCILIATION_REQUIRED with authenticated
charge reference/amount/currency, preserving canonical identity and existing intent. Their automated
refund is blocked by the canonical-only, logical-amount cap; see item 19. Non-cancelled expired or
cutoff/frozen captures preserve the established reconciliation behavior. No paid event is discarded
merely to force a customer DTO or reopen a collection.

## 16. Read consistency

One shared loader uses four set queries (Payment, Attempts, Refunds, reconciliation), independent of
page length. Standalone owned-request read needs at most five SELECTs, payment retry freeze check
at most six, excluding existing live authentication reads. No per-row financial SQL or provider
calls. Embedded and standalone DTOs reuse the same projections and coherent transaction snapshot.
Payment/quote currency/amount, known states, canonical succeeded Attempt/charge/time, refund target,
currency and balance are validated. Accepted/planned/completed collection without canonical funds
is an inconsistency. Unsafe persistence is a sanitized 503. Historical refund order and timestamps,
actual uncertainty, no-store and repeated-read absence of writes are tested.

## 17. Migration

`0019_customer_financial_events`, parent `0018_customer_mobile_core`, adds nullable
`provider_payment_id varchar(200)`, `amount_minor bigint`, `currency char(3)` on provider events,
with optional-positive amount CHECK. No new table/status/infrastructure or monetary cap change.
Old events remain valid; no guessed backfill. Verified upgrade/downgrade/upgrade retains an existing
event, confirms nullability, and rejects zero amount. Downgrade drops only these metadata columns
and CHECK, retaining original rows; new capture facts would be lost, so an evidence-preserving
rollback must export them first. Tests apply migrations only to the disposable local database.

## 18. Verification results

- Ruff lint: passed; Ruff format check: 274 files unchanged/formatted.
- mypy: passed, 141 source files. git diff --check: passed.
- Unit suite: 438 passed, 1 existing Windows/POSIX skip, 4 existing Firebase SDK deprecation warnings;
  40.08 seconds.
- Focused Batch B PostgreSQL + migration suite: 51 passed, zero failures; 96.74 seconds.
  After strengthening the wait barriers and correcting legacy fixtures, the targeted four race
  directions and two corrected reads passed again: 6 passed, 60 deselected; 18.28 seconds.
- Final complete PostgreSQL-backed regression on the final implementation: **924 passed, 1 skipped,
  zero failures**, 4 existing Firebase SDK warnings; 777.08 seconds. JUnit confirms **486 PostgreSQL
  integration tests passed** and **438 unit tests passed / 1 skipped**. This includes the legacy
  database tests without an integration marker, plus auth, booking, provider, financial, planning,
  compaction, fulfilment, customer projection and migration regressions.
- First complete run: 922 passed, 2 failed, 1 skipped; 715.97 seconds. Both failures were older
  COMPLETED/PRE_PLANNING fixtures with no canonical settled payment. Those fixtures were corrected
  and the entire suite was repeated successfully above, without weakening the financial invariant.
- Alembic `heads` / `current`: single `0019_customer_financial_events` head. Upgrade/downgrade/
  upgrade and positive amount CHECK are covered by passing PostgreSQL migration tests.
- **Unfiltered `alembic check` failed** on three existing metadata discrepancies: extension-owned
  `spatial_ref_sys`, `ix_collection_request_pickup_location_gist` and
  `ix_collection_request_planning_batch_id`. The two indexes are already created by migration 0007
  in base main and are absent from its ORM index declarations; these defining files/env are unchanged.
  No proposed removal was applied. A separate metadata comparison excluding exactly these three
  proven baseline objects **passed with zero remaining schema differences**. This baseline Alembic
  tooling issue remains an explicit limitation.

Earlier focused runs found a stale replay reload defect (fixed) and fixtures that lacked canonical
charge facts or created a second unsettled attempt. Those were corrected to represent real settled
or failed-then-late-capture history. Worker concurrency initially asserted global pool emptiness while
another worker was in a short DB phase; final assertion observes both HTTP calls at a common barrier.
All final test results and the remaining unfiltered Alembic tooling failure are recorded above.

## 19. Precise unresolved gaps

1. Separate additional charge: canonical-only Refund targets plus the logical Payment amount cap
   prohibit its automatic payout. Minimal authenticated event facts are retained, but per-charge
   liability/refund accounting requires approval. Do not claim the universal “no unrefunded late
   obligation” criterion is met for this exception or enable unrestricted compensation on that basis.
2. A non-cancelled expired/cutoff/frozen first capture remains the existing reconciliation exception;
   this full customer-cancellation policy does not authorize its commercial disposition/recovery.
3. Legacy CANCELLED records lacking intents are not silently backfilled by command replay. A
   reviewed, audited recovery procedure and ownership of unresolved provider events are required.
4. Definitively failed cancellation refunds remain FAILED history; inventing another operation/key
   on ordinary cancellation replay is not authorized. Operations resolution needs a defined policy.
5. OPERATIONS_ADJUSTMENT cannot be accurately represented by the frontend's three reasons. Such
   financial reads fail safely; an approved public reason/metadata contract is needed. No reason
   was relabelled to hide this mismatch.
6. Partial pre-existing reserved refund cases refuse cancellation with REVIEW_REQUIRED/NOT_ELIGIBLE
   until an explicit remaining-balance commercial policy is approved.

These specific unsafe paths remain stopped/reconciled. Other approved canonical-charge behavior is
implemented and reviewable in this branch. ADR/domain updates identify the user-approved unsettled
cancellation edge and full-refund policy explicitly, without changing planning/compaction policy.

## 20. Frontend untouched

The frontend was verified clean at preflight and during implementation on the SHA above.
This Batch B task performed only reads there: no edits, DTO changes or capability enablement.
At the final post-commit check, new local frontend changes were observed in README/package files,
runtime/notification integration and notification tests, plus untracked `.node-version`,
`IOS_EXPO_GO_REPORT.md`, notification environment and runtime test files. They appeared during the
run and are outside this backend commit; their authorship is not attributed here. They were neither
modified nor reverted by this task. The backend working tree is clean; final frontend cleanliness
must not be claimed. Its existing local work is preserved.

## 21. Infrastructure and prior work untouched

No Terraform, Azure resources, provider adapter, OTP, geocoding/H3, operational availability, pricing,
catalogue, compaction, dispatch, evidence/media or deployment changes. Existing modular monolith,
PostGIS, Service Bus Standard, refund worker/native provider idempotency and cost structure retained.
The only runtime policy extension is the user-approved customer cancellation compensation.

## 22. Nonprod/provider prerequisites and review stop

Architectural review must resolve item 19 and approve deployment of migration 0019. Audit legacy
cancellations, failed refunds and unresolved late/additional-charge events before enabling the flow.
Configure planning lead time/command retention, existing Razorpay test account, auto-capture,
refund/webhook subscriptions and secrets. Validate real nonprod capture/cancel ordering, refund
processed/failed/uncertain callbacks, lost-response recovery, duplicate events and end-to-end
RefundRequested publisher/queue/RBAC/worker delivery. Confirm exact DTOs and UI states against the
unchanged frontend, then enable capabilities only after acceptance. No live/sandbox provider call,
cloud migration, deployment, infrastructure provisioning, push or merge was performed here.

Stop after the local commit; await architectural review.
