# Phase 1V — Production Razorpay payment/refund runtime

Base: `744be209e15b6af9cb5d8fb2dad90a5b8748889a` (verified origin/main).
Branch: `phase1/razorpay-payment-runtime`. No migration; existing finance/reliability schema suffices.

## Production review (before new tests)

| Invariant / expected behavior | Production enforcement | DB protection / failure boundary |
|---|---|---|
| Durable attempt precedes HTTP | `initiate_payment_attempt` T1 commits | command uniqueness + resource ID; crash resumes CREATED |
| One recoverable order per attempt | `RazorpayProvider.initiate_payment`, `_find_order`, `_order` | immutable UUID receipt `pa_<hex>`; provider receipt uniqueness; duplicate/lost POST recovers by receipt |
| No blind/random order retry | `_request`, `initiate_payment` | one POST plus bounded recovery read; unavailable recovery remains uncertain |
| Invalid credentials are unavailable, not commercial failure | `razorpay_configured`, `_request` | partial configuration fails before startup; durable attempt/refund remains retryable |
| Forged webhook cannot mutate DB | `authenticate_webhook` before domain call | HMAC exact bytes, constant-time compare; required bounded event ID |
| Duplicate event has no second effect | `process_authenticated_payment_event` | `(provider, external_event_id)` unique insert arbitration |
| Notes cannot select a payment | adapter ignores payment notes; domain correlates persisted references | unique provider order/payment references; conflicting mapping reconciles |
| Wrong amount/currency cannot accept | domain validates event against locked Payment | Payment FOR UPDATE; mismatch returns reconciliation before state writes |
| Authorization/unrelated events are harmless | IGNORED outcome, early domain return | metadata-only UNMATCHED event; no business writes |
| Failed-before-captured may converge; stale failure cannot corrupt success | domain success/reference checks before writes | sorted attempt locks + Payment lock; successful ID preserved; distinct captured ID reconciles |
| One canonical acceptance | existing canonical replay/additional-success paths | Payment lock + conditional request transition + unique acceptance outbox key |
| Freeze/cutoff/expiry remain authoritative | existing work-unit domain path | same advisory lock, batch check, cutoff/expiry; no automatic refund |
| Authorized refund cannot double-execute financially | `execute_refund_provider_call`, adapter | Refund lock in short T1; PROCESSING/UNCERTAIN resume exact `rf_<hex>` native key/body |
| Stale worker cannot overwrite terminal webhook truth | refund result T2 re-lock/re-read | terminal guard + provider-ID conflict guard; contradictory result retained as uncertain |
| Refund webhook must match canonical charge/amount/currency | `_process_refund_event` | Refund locks + unique provider refund ID; conflicting identity reconciles |
| Inbox PROCESSING resumes and uncertainty retries | `process_refund_message` | durable claim before execution; only SUBMITTED/SUCCEEDED/FAILED complete inbox |
| Three explicit outbox routes send before PUBLISHED | `publish_outbox_batch` | bounded due selection; short mark transaction after actual send; unknown types excluded |
| No locks across provider I/O | payment/refund service closes sessions before adapter call | T1 -> closed session -> HTTP -> T2 |
| PII/secrets/raw bodies never persist/log | adapter extracts allow-listed scalar facts only | event stores hash/IDs/type/state; messages contain refund ID only; sanitized exceptions |

## Provider contracts verified

- [Orders receipt lookup](https://razorpay.com/docs/api/orders/fetch-all/) supports receipt filtering. Exact receipts are checked locally; malformed/saturated/ambiguous collections fail uncertain.
- [Order creation](https://razorpay.com/docs/api/orders/create/) requires unique receipt; duplicate creation is recovered with the same receipt, never a new one.
- [Normal refund idempotency](https://razorpay.com/docs/api/refunds/normal-refunds-idempotent/) uses `X-Refund-Idempotency`, identical body and key; in-progress 409 is retryable uncertainty.
- [Webhook validation](https://razorpay.com/docs/webhooks/validate-test/) authenticates exact bytes and supplies event identity.
- [Payment events](https://razorpay.com/docs/webhooks/payments/) / [refund events](https://razorpay.com/docs/webhooks/refunds/) supply provider facts. Only captured payment is successful; refund.created remains non-terminal.

## Runtime sequence

Customer attempt command commits UUID/key/idempotency result, closes DB, looks up stable receipt,
creates if absent, recovers ambiguous POST, then persists validated order. Checkout UI is out of scope.
HMAC-authenticated payment.captured resolves persisted order/payment references, checks exact integer
amount and currency, then uses existing payment/work-unit serialization to accept atomically.

Already-authorized Refund rows append identifier-only RefundRequested. Explicit publisher route
sends to configured refund queue. Dedicated Peek-Lock worker uses workload identity, no prefetch,
AutoLockRenewer, resumable inbox and native idempotent normal refunds outside DB transactions.
Crash before HTTP or after remote commit resumes identical key/body. Ambiguous outcome abandons
broker message and does not complete inbox. This is at-least-once delivery, not exactly-once network I/O.

## Production operations

Activate account/KYC; inject matching Test/Live merchant keys, webhook secret and bounded timeout
through runtime secrets; enable Dashboard auto-capture (no manual capture implementation).
Configure existing webhook URL `/v1/payments/provider/webhook` and subscribe payment.captured,
payment.failed, refund.processed, refund.failed. Provision a dedicated refund queue, worker workload
identity/RBAC and positive renewal/operation timeouts; include refund queue in publisher settings.
Run the existing finite publisher via `python -m tirodhan.workers.outbox_publisher` and the dedicated
consumer via `python -m tirodhan.workers.refunds`. Inject TIRODHAN_RAZORPAY_KEY_ID,
TIRODHAN_RAZORPAY_KEY_SECRET, TIRODHAN_RAZORPAY_WEBHOOK_SECRET,
TIRODHAN_RAZORPAY_HTTP_TIMEOUT_SECONDS, TIRODHAN_REFUND_QUEUE_NAME and
TIRODHAN_REFUND_LOCK_RENEWAL_SECONDS; preserve existing Service Bus namespace/identity/timeouts.
Review broker retry/DLQ operations and reconciliation alerts. No deployment Terraform in this phase.
Cancellation/additional/late-success refund policy, pricing, and retry/retention product values remain open.

## Verification

Clean main baseline: 515 passed (four existing Firebase deprecation warnings).
Final focused run: 176 passed in 64.78s — 77 new provider/worker unit cases,
46 new PostgreSQL runtime cases, and 53 existing financial/serviceability/dispatch regressions.
Final complete `pytest -q` with TIRODHAN_TEST_DATABASE_URL: 638 passed in 278.44s,
no skips/failures (374 integration + 264 unit cases). Four pre-existing Firebase SDK deprecation
warnings remain; unrelated push implementation was not changed.

`ruff check .`: passed. `ruff format --check .`: 204 files formatted.
`mypy`: passed, 109 source files. `git diff --check`: passed.
`alembic heads`: one unchanged head, `0017_fleet_dispatch_notification`.
No migration was needed or created. PostgreSQL/PostGIS tests use existing disposable local DB.

Focused command: pytest -q tests/unit/test_razorpay.py tests/unit/test_refund_worker.py
tests/integration/test_razorpay_payments.py tests/integration/test_razorpay_refunds.py
tests/integration/test_razorpay_publisher.py tests/integration/test_collection_payment.py
tests/integration/test_cancellation_refunds.py tests/integration/test_serviceability_runtime.py
tests/integration/test_dispatch_routing_planning.py.

## Exact change inventory

Added:

- `docs/PHASE_1V_COMPLETION.md`
- `src/tirodhan/modules/payments/razorpay.py`
- `src/tirodhan/modules/payments/refund_consumer.py`
- `src/tirodhan/modules/payments/runtime.py`
- `src/tirodhan/workers/refunds.py`
- `tests/integration/razorpay_helpers.py`
- `tests/integration/test_razorpay_payments.py`
- `tests/integration/test_razorpay_publisher.py`
- `tests/integration/test_razorpay_refunds.py`
- `tests/unit/test_razorpay.py`
- `tests/unit/test_refund_worker.py`

Modified:

- `.env.example`
- `docs/ARCHITECTURE.md`
- `docs/DOMAIN_MODEL.md`
- `docs/IDEMPOTENCY.md`
- `docs/PROJECT_CONTEXT.md`
- `docs/adr/ADR-005-payments-refunds.md`
- `src/tirodhan/api/routes/payments.py`
- `src/tirodhan/core/config.py`
- `src/tirodhan/main.py`
- `src/tirodhan/modules/payments/ports.py`
- `src/tirodhan/modules/payments/refunds.py`
- `src/tirodhan/modules/payments/service.py`
- `src/tirodhan/modules/reliability/publisher.py`
- `src/tirodhan/modules/reliability/service_bus.py`
- `src/tirodhan/workers/outbox_publisher.py`

Shared-file diff reviewed against main: only financial config/lifespan wiring, controlled malformed
webhook handling, correlation validation, resumable refund execution, explicit refund routing and
delivery settlement adapter were changed. Existing OTP/serviceability/media/dispatch code and
tests are unchanged. No new public routes, migrations, dependencies or deployment resources.

## Final adversarial production-code review

1. Lost Orders POST cannot create a new logical order: stable receipt lookup/uniqueness/recovery.
2. Five webhook deliveries arbitrate on provider-event uniqueness; one acceptance/outbox effect.
3. Forged webhook fails HMAC before any DB call.
4. Captured amount/currency must exactly match locked Payment before business state mutation.
5. payment.authorized is IGNORED, never success.
6. Stale failure skips successful attempt reference/state writes.
7. Distinct captured charge reconciles before replacing established successful ID.
8. Remote refund commit/local crash resumes identical native key/body, not another logical refund.
9. PROCESSING resumes after crash; it is not a permanent skip.
10. Refund T2 re-locks and preserves terminal webhook truth.
11. RefundRequested uses only configured refund queue; other two explicit routes remain intact.
12. Provider HTTP occurs after short DB session exits, before a new persistence transaction.
13. Only controlled errors/identifier metadata persist; raw bodies, credentials and PII do not log.
14. No automatic refund policy was introduced; only explicitly authorized intents execute.

Legacy provider-neutral fixtures without financial facts retain their established canonical replay
semantics; real Razorpay capture always carries validated financial facts and detects a distinct
successful provider charge. This compatibility does not weaken Razorpay validation.

Normal tests use the real adapter with injected HTTP transports and real PostgreSQL/PostGIS, never
live financial calls. Production paths use actual Razorpay/Service Bus APIs, not simulation ports.
Live merchant/queue smoke verification belongs to deployment, with explicit Test/Live credentials;
this phase does not claim a live charge/refund was performed. No unresolved implementation conflict.
