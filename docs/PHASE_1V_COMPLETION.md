# Phase 1V — Razorpay Production Payment and Refund Integration

Branch: `phase1/razorpay-production-provider`.
Base main: `744be209e15b6af9cb5d8fb2dad90a5b8748889a` (clean and pulled before coding).
Baseline: 515 passed, four existing Firebase SDK deprecation warnings; financial integration and
concurrency tests were enabled against disposable PostgreSQL/PostGIS before production edits.

## Delivered flow

RazorpayProvider implements both existing financial ports using HTTPX, not the Razorpay SDK.
Payment initiation reads the durable logical payment amount/currency and creates an Order with
`ta_<attempt UUID hex>`, partial payment disabled and one internal-ID note. Validated responses
populate existing generic provider columns. Configuration failure is unavailable/configuration,
not a customer's failed payment; validated amount rejection is FAILED; ambiguous HTTP/transport
or malformed-success results retain INITIATION_UNCERTAIN. No silent HTTP retries are enabled.

The new authenticated CUSTOMER checkout-confirmation endpoint verifies HMAC-SHA256 against the
**stored** Order and associates the verified payment reference. Same reference replays naturally
under the attempt row lock; another reference conflicts. It neither accepts a request nor marks
Payment successful. The existing attempt endpoint remains the Checkout launch contract.

The existing webhook endpoint verifies the raw-body signature before parsing. Documented
`x-razorpay-event-id` supplies deduplication; body SHA-256 provides fallback for exact retries without
a valid header. Captured/paid events validate stored Order, amount and currency, including correlation
without an internal note. Conflicting identities/facts reconcile. Authorized/unknown/malformed
authenticated events are durably UNMATCHED rather than success. Canonical repeat success is normally
processed even after planning freeze, another successful attempt is additional-charge reconciliation,
and late/cutoff/frozen success cannot resurrect eligibility. Success and later failure retain success.

Refunds always target the canonical successful provider payment and send the explicit requested
minor-unit amount at normal speed. The documented Payment Gateway X-Refund-Idempotency header
contains SHA-256 hex of the persisted internal key. Validated `processed`/`pending`/`failed` responses
map to SUCCEEDED/SUBMITTED/FAILED. Refund-created/processed/failed webhooks validate and correlate
existing refund/payment references and financial facts, preserving terminal contradictions for
reconciliation. No raw financial payload, secret, signature or instrument data is persisted/logged.

RefundRequested now routes through the finite publisher allow-list to a dedicated Service Bus queue.
Serviceability and dispatch routes are unchanged and still send before PUBLISHED. The thin refund
worker uses Peek-Lock, zero prefetch, AutoLockRenewer and identifier-only envelopes; domain execution
and inbox recovery live in the payment module. Network I/O always occurs outside DB transactions.

## Crash, replay and concurrency

| Boundary | Durable outcome and retry |
|---|---|
| Order command before creation commit | Normal transaction rollback; no provider call |
| Created Order attempt, concurrent replay or interrupted creator | Replay locks attempt, conservatively records uncertainty, never issues a second POST |
| Order timeout/lost response/local post-call failure | Durable same attempt; reconciliation, not blind POST |
| Known original Order result after conservative replay | Can persist on CREATED/INITIATION_UNCERTAIN, never overwrite terminal webhook success |
| Refund inbox commit while refund still PENDING | PROCESSING inbox resumes the PENDING refund safely |
| Refund PROCESSING commit followed by interruption/ambiguous execution | Redelivery records INITIATION_UNCERTAIN and completes inbox; no second POST |
| Overlapping refund deliveries | One PENDING claimant; another delivery conservatively reconciles, never invokes again |
| Known original refund result after conservative recovery | Can resolve uncertainty; terminal webhook outcomes remain authoritative |
| Terminal refund / inbox commit before broker settlement | Redelivery settles without another provider call |
| Publisher send followed by local crash | Same outbox UUID may be redelivered; business/inbox safeguards apply |

Native refund idempotency is additional protection. This phase deliberately does not automatically
retry ambiguous financial POSTs or claim exactly-once network execution.

## Production invariant matrix

Test names below are the adversarial tests, not production substitutes. HTTP responses are mocked;
the adapter, HMAC, domain transitions and database concurrency execute their actual code.

| Invariant | Production function | DB protection / transaction boundary | Adversarial evidence |
|---|---|---|---|
| 1. Order amount is server-owned | `initiate_payment_attempt` → `RazorpayProvider.initiate_payment` | Durable Payment read in closed session before HTTP | `test_order_request_response_and_stable_receipt`; `test_uncertain_or_interrupted_order_does_not_blindly_post_again` |
| 2. Merchant secret never reaches frontend | `RazorpayProvider`, `post_payment_attempt`, `post_checkout_confirmation` | SecretStr settings; explicit response fields, no secret persistence | `test_missing_partial_and_secret_configuration`; `test_checkout_api_ownership_stored_order_signature_and_no_business_success` |
| 3. Order call has no DB transaction | `initiate_payment_attempt` | T1 commits creation/idempotency; read session closes; T2 persists result | `test_uncertain_or_interrupted_order_does_not_blindly_post_again` checks zero checked-out DB connections and committed attempt during HTTP |
| 4. Uncertain Order cannot be blindly replayed | `initiate_payment_attempt` | Claim-creator guard + replay row lock, existing command uniqueness | `test_concurrent_order_replay_invokes_provider_once`; `test_uncertain_or_interrupted_order_does_not_blindly_post_again` |
| 5. Checkout cannot accept booking | `confirm_checkout` | Owned attempt row only; no Payment/request/outbox writes | `test_checkout_api_ownership_stored_order_signature_and_no_business_success` |
| 6. Raw HMAC precedes JSON | `RazorpayProvider.authenticate_webhook` | Authentication occurs before event transaction | `test_raw_webhook_signature_and_event_mapping`; `test_app_real_runtime_webhook_hmac_failure_and_exact_body_no_pii_persistence` |
| 7. Authorized is not success | `RazorpayProvider._map_event` | IGNORED → UNMATCHED event only | `test_authenticated_unknown_events_are_acknowledged_and_deduplicated`; `test_authenticated_conflicting_facts_never_accept` |
| 8. Success Order matches stored attempt | `process_authenticated_payment_event` | Sorted correlation row locks; provider-order unique index | `test_authenticated_conflicting_facts_never_accept` (Order/internal identity conflicts) |
| 9. Amount/currency match logical Payment | `process_authenticated_payment_event` | Locked Payment validates facts before financial transitions | `test_authenticated_conflicting_facts_never_accept` (amount/currency) |
| 10. Duplicate webhook cannot repeat business effects | `process_authenticated_payment_event` | Unique provider/event + Payment lock + conditional request transition + unique outbox key in one transaction | `test_real_adapter_webhook_atomic_success_duplicates_and_later_failure`; existing atomic rollback regression |
| 11. Additional charge remains reconciliation | `process_authenticated_payment_event` | Logical Payment lock preserves one canonical successful attempt | `test_additional_success_and_concurrent_success_keep_one_canonical_attempt`; existing late/freeze/cutoff/window regressions |
| 12. Refund uses canonical provider payment | `create_refund`, `execute_refund_provider_call` | Payment lock validates canonical attempt and reserved refund sum | `test_refund_delivery_result_durable_duplicates_and_no_second_post`; existing over-refund/concurrent reservation tests |
| 13. Ambiguous refund cannot duplicate money movement | `execute_refund_provider_call`, `process_refund_message` | Refund row claim/recovery + inbox + stable documented external key | `test_interrupted_processing_commit_redelivery_reconciles_without_second_call`; `test_known_provider_result_local_rollback_does_not_repeat_money_movement`; `test_concurrent_refund_messages_do_not_duplicate_invocation` |
| 14. Refund HTTP holds no DB transaction | `execute_refund_provider_call` | PROCESSING commits before HTTP; result stored in separate transaction | `test_refund_delivery_result_durable_duplicates_and_no_second_post` checks zero checked-out connections and PROCESSING during HTTP |
| 15. Existing publisher routes still send | `publish_outbox_batch` | Explicit allow-list; send succeeds before marking PUBLISHED | `test_three_explicit_routes_send_before_published_and_unknown_stays_pending`; existing dispatch/serviceability regressions |
| 16. Provider-neutral schema suffices | All above | Existing generic columns, FKs/unique indexes and reliability tables | Existing migration suite; unchanged single Alembic head; no migration added |

## Runtime configuration / Operations

Configure together:

- `TIRODHAN_RAZORPAY_KEY_ID` (public Checkout configuration, **not** a secret key).
- `TIRODHAN_RAZORPAY_KEY_SECRET` and `TIRODHAN_RAZORPAY_WEBHOOK_SECRET` through deployment secrets /
  Key Vault references; no application Key Vault calls.
- `TIRODHAN_RAZORPAY_HTTP_TIMEOUT_SECONDS`, finite within `(0, 60]` seconds.
- `TIRODHAN_RAZORPAY_API_BASE_URL`, HTTPS; default `https://api.razorpay.com/v1`.
- `TIRODHAN_REFUND_QUEUE_NAME` and positive `TIRODHAN_REFUND_LOCK_RENEWAL_SECONDS` for the worker.
- Existing namespace/workload identity/operation timeout/outbox batch settings for broker runtimes.

No Razorpay configuration preserves the existing controlled unconfigured-provider behavior. Partial
or invalid configuration fails clearly. Owned clients close during shutdown; injected clients remain
caller-owned. No startup provider network call is made.

Run the finite publisher with `python -m tirodhan.workers.outbox_publisher`; the separate receiver is
`python -m tirodhan.workers.refunds`. Queue hosting/scaling/deployment is not implemented here.

Operations must enable Orders auto-capture in the correct Razorpay mode, supply matching merchant
credentials, configure the existing `/v1/payments/provider/webhook` URL and independent webhook
secret, and subscribe to payment.captured, payment.failed, order.paid, refund.created,
refund.processed and refund.failed. Dashboard configuration is an operational prerequisite, not
application-enforced infrastructure. Reconciliation uses retained references/state and provider
Dashboard/API evidence; no reconciliation automation or cancellation refund policy was invented.

## Verification

- Focused adapter/worker/financial/routing/serviceability run: **153 passed** (58 unit,
  95 PostgreSQL/PostGIS integration), including existing financial concurrency and atomicity tests.
- Complete suite with disposable PostgreSQL/PostGIS enabled: **615 passed**, no skips/failures
  (370 integration, 245 unit), in 259.14 seconds. The four warnings are existing Firebase SDK
  MulticastMessage.tokens deprecations, unrelated to financial changes.
- `ruff check .`: passed.
- `ruff format --check .`: passed, 204 files already formatted.
- `mypy`: passed, 110 production source files.
- `git diff --check`: passed.
- `alembic heads`: one head, `0017_fleet_dispatch_notification`.

No migration added; provider-neutral schema was sufficient. No live Razorpay traffic or merchant
account is needed for tests. The work is committed on the requested branch, not merged.

## Changed-file inventory

Added:

- `src/tirodhan/modules/payments/razorpay.py`
- `src/tirodhan/modules/payments/runtime.py`
- `src/tirodhan/modules/payments/checkout.py`
- `src/tirodhan/modules/payments/refund_consumer.py`
- `src/tirodhan/workers/refunds.py`
- `tests/unit/test_razorpay.py`
- `tests/unit/test_refund_worker.py`
- `tests/integration/test_razorpay_payments.py`
- `tests/integration/test_razorpay_refunds.py`
- `tests/integration/test_razorpay_publisher.py`
- `docs/PHASE_1V_COMPLETION.md`

Updated:

- `src/tirodhan/modules/payments/ports.py`
- `src/tirodhan/modules/payments/service.py`
- `src/tirodhan/modules/payments/refunds.py`
- `src/tirodhan/api/routes/payments.py`
- `src/tirodhan/core/config.py`
- `src/tirodhan/main.py`
- `src/tirodhan/modules/reliability/publisher.py`
- `src/tirodhan/modules/reliability/service_bus.py`
- `src/tirodhan/workers/outbox_publisher.py`
- `.env.example`
- `docs/PROJECT_CONTEXT.md` (remove the now-settled gateway from open decisions)
- `docs/ARCHITECTURE.md`
- `docs/DOMAIN_MODEL.md`
- `docs/IDEMPOTENCY.md`
- `docs/adr/ADR-005-payments-refunds.md`

## Remaining human/operational decisions

No new architecture decision is unresolved. Existing commercial customer-cancellation refund policy,
financial retention/reconciliation procedures and production pricing remain outside this phase.
Production requires merchant/Dashboard configuration and dedicated queue deployment; no Terraform,
frontend Checkout UI, premium refunds, multi-gateway routing or orchestration is included.
