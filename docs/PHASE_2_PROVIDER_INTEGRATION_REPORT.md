# Phase 2: 2Factor and Razorpay integration report

Implementation and official-documentation verification date: 2026-10-09.
This delivery ends at a local commit for architectural review. Mocked HTTP transports are used
throughout; no SMS, payment, refund, Azure migration or deployment was performed.

## 1. Workspace and delivery

Branch: `phase2/2factor-razorpay-runtime`.
Clean backend `main` base: `fd27e42c20f7085b9d1b6fc54c56c28ec4ea996c`.
The merged base contains Batch A `2634ecb62294ce1202925da37db006cca7bd83b0` and the approved
Delhi slot correction `ea689b5875003b853fc2160f5af93a8f3d3e9611`.
The final commit SHA is supplied in the delivery message and `git log -1`; embedding this report's
own commit ID would be self-referential. No push, PR, merge or deployment is authorized or performed.

Read-only frontend: `C:\Users\91956\Tirodhan-frontend\tirodhan-frontend`, clean `main` at
`103d181eae4ebc539f8253cb009ac1b42c953e12`. Its documented proposal in
`apps/customer-mobile/docs/CUSTOMER_MOBILE_BACKEND_CONTRACTS.md` (checkout section) and
`apps/customer-mobile/src/api/customer-contracts.ts` supplies the exact handoff contract.

## 2. Exact changed files

- `.env.example`
- `docs/adr/ADR-007-identity-authentication.md`
- `docs/PHASE_2_PROVIDER_INTEGRATION_REPORT.md`
- `src/tirodhan/api/router.py`
- `src/tirodhan/api/routes/checkout.py`
- `src/tirodhan/core/config.py`
- `src/tirodhan/main.py`
- `src/tirodhan/modules/identity/runtime.py`
- `src/tirodhan/modules/identity/twofactor.py`
- `src/tirodhan/modules/payments/razorpay.py`
- `tests/integration/test_authentication_challenge.py`
- `tests/integration/test_checkout_handoff.py`
- `tests/integration/test_twofactor_authentication.py`
- `tests/unit/test_otp_runtime.py`
- `tests/unit/test_twofactor.py`

## 3–6. OTP integration, configuration and compatibility

The new adapter implements these operations with percent-encoded path segments:

| Operation | Method and fixed-origin path | Accepted result |
| --- | --- | --- |
| Start | `GET https://2factor.in/API/V1/{api_key}/SMS/{phone}/AUTOGEN` | JSON object, exact `Status=Success`, nonempty opaque `Details` reference using ASCII letters/digits/underscore/hyphen, maximum 200 characters |
| Start with approved template | Same path with `/{template_name}` appended | Same result; configured template is encoded as one segment |
| Verify | `GET https://2factor.in/API/V1/{api_key}/SMS/VERIFY/{session_id}/{otp}` | Only exact `Status=Success` and `Details=OTP Matched` on successful HTTP response |

Tirodhan generates no OTP. The existing challenge operation encrypts the phone, persists the
provider reference and provider identity code, and returns only its own UUID `challenge_reference`. All five public
auth routes and their DTOs are unchanged. The identity/JWT/session/RBAC code is unchanged.

Required selected-provider configuration (environment prefix `TIRODHAN_`):

| Setting | Behavior |
| --- | --- |
| `OTP_PROVIDER` | Explicit `2FACTOR` or `KALEYRA_VERIFY`; absent means unconfigured, never infer from credentials |
| `TWOFACTOR_API_KEY` | Required `SecretStr` for selected 2Factor; blank/unsafe local configuration fails closed |
| `TWOFACTOR_HTTP_TIMEOUT_SECONDS` | Required finite timeout, greater than zero and at most 60 seconds; implementation bound, not vendor SLA |
| `TWOFACTOR_TEMPLATE_NAME` | Optional; only set after the configured account approves the exact template/sender workflow |
| `AUTH_OTP_CHALLENGE_TTL_SECONDS` | Existing required local lifetime; deliberately no guessed vendor lifetime default |

Existing Kaleyra adapter, five credentials/flow settings and POST Generate/Validate operations
remain intact. Deployments using them must add `OTP_PROVIDER=KALEYRA_VERIFY`. Neither provider is
automatically retried through the other. Existing tests are updated for explicit Kaleyra selection.
Configuration switches fail before HTTP if an existing challenge belongs to another provider.

HTTP 401/403 maps to `OtpProviderConfigurationError`, 429 to `OtpProviderRateLimitedError`, and
transport/redirect/5xx/malformed/unsupported results to `OtpProviderUnavailableError`. A negative
verification envelope (`Status=Error`, string `Details`) on HTTP 200 or 400 maps only to generic
`OtpVerificationError`. Its wording is not parsed into guessed mismatch/expiry/account codes.
No specific provider `OtpExpiredError` is claimed. Local challenge expiry continues to reject
verification before provider invocation. All failures prevent session creation.

The reusable application-owned async client uses `retries=0` and closes at shutdown; injected
clients remain caller-owned. The adapter enforces fixed HTTPS origin, no redirects and explicit
per-request timeouts. Existing `httpx`/`httpcore` log suppression also applies at DEBUG. Request
errors and JSON decoding failures have sanitized exceptions with suppressed chaining; key, phone,
OTP and session paths never enter application logs or API errors.

OTP idempotency remains the existing domain strategy: `auth.start` reserves a client UUID with a
phone-HMAC fingerprint before sending outside the transaction. Only the reservation owner sends.
Exact completed replay returns the active challenge; concurrent or ambiguous same-key commands
return 409 without another send. A remote send followed by local persistence failure retains the
reservation. Recovery requires an explicit new command ID. Verification revalidates and locks the
challenge after provider success; identity, session, consumption and `auth.verify` commit together.
Completed login replay never calls the provider or issues another session/credential response.

## 7–9. Razorpay verification and minimal changes

The existing adapter, business services, event persistence, refund consumer and worker were read.
Verified operations remain unchanged:

| Operation | Existing implementation retained |
| --- | --- |
| Create order | Basic-auth `POST https://api.razorpay.com/v1/orders`; persisted integer amount/currency, `pa_<attempt.hex>` receipt, `partial_payment=false`, internal attempt UUID notes |
| Lookup/recover order | Basic-auth `GET /v1/orders?receipt=...&count=100`; exact receipt selection despite broader filter semantics; conflicting identities/money, malformed collections or saturated page fail uncertain |
| Payment events | Raw-body authenticated `payment.captured`/`payment.failed`; no native callback or customer charge-ID acceptance command |
| Normal refund | Basic-auth `POST /v1/payments/{payment_id}/refund`; persisted amount, `speed=normal`, `rf_<refund.hex>` receipt and `X-Refund-Idempotency`, controlled internal UUID notes |
| Refund events | Authenticated `refund.created`, `refund.processed`, `refund.failed` correlated to durable records |

There are no changed or new outbound Razorpay methods. The sole adapter addition is access to its
configured **public** key ID for the new checkout projection. Key secret and webhook secret remain
private. No SDK or dependency was introduced.

Orders use durable attempt-first persistence before external I/O. One invocation first looks up
the receipt, sends at most one POST if no exact match is visible, then performs one read-only
recovery on uncertain create. A missing recovery result remains `INITIATION_UNCERTAIN`; it is not
proof that no remote order exists. Subsequent command replay uses the same attempt/receipt. Current
official create-order documentation describes duplicate-receipt rejection/idempotency, supporting
this existing recovery design. No invented order idempotency header or unconditional POST retry
was added. Returned entity, ID, receipt, amount and currency must match persisted obligations.

## 10. Native checkout handoff

Added `GET /v1/customer/payment-attempts/{payment_attempt_id}/checkout`, matching the frontend
proposal exactly. Response fields: `request_id`, `payment_attempt_id`, `provider=RAZORPAY`,
`public_key_id`, `provider_order_id`, `merchant_display_name`, positive JavaScript-safe
`amount_minor`, `currency=INR`, RFC3339 `expires_at`.

`TIRODHAN_RAZORPAY_MERCHANT_DISPLAY_NAME` is new explicit display configuration. Public key comes
from the active Razorpay adapter. Order, amount, currency and expiry come from persisted owned
financial/request records; no customer amount or prefill PII is accepted. Existing attempt creation
DTO remains unchanged. This read creates no order, attempt, charge, reservation or idempotency row.

The route uses existing live CUSTOMER authentication and a read-only repeatable-read transaction,
with `Cache-Control: private, no-store`. Absent/foreign attempt is 404. Expired, uncertain, failed,
paid/accepted or otherwise ineligible handoff is 409; planning cutoff/freeze returns the existing
specific conflict code. Missing provider/display/planning configuration or inconsistent financial
data fails 503. Responses use the established error envelope. Native success can only prompt a
status refresh; it does not accept a collection. The frontend adapter and capability gate remain
untouched, so this backend endpoint alone does not enable native checkout in the app.

## 11–12. Webhooks, reconciliation and refunds

Existing webhook authentication verifies `X-Razorpay-Signature` as HMAC-SHA256 over exact raw bytes
with constant-time comparison. Event ID/type/shape/references are validated. Database uniqueness
deduplicates `(provider, external_event_id)`; only minimal identifiers, outcome and payload hash
persist, not a raw webhook body. Canonical order/payment correlation and persisted amount/currency
are checked; arbitrary notes cannot select an unrelated payment.

Payment locking, the shared work-unit lock and collection revalidation arbitrate capture,
cancellation and planning. Only verified capture satisfying pending request, expiry and freeze
checks commits `PENDING_PAYMENT -> ACCEPTED` and its outbox event. Late/second/mismatched captures
are recorded for reconciliation without accepting the request. Stale failure cannot downgrade
captured payment. Duplicate deliveries have one business effect. No manual capture operation was
added; configure and verify the merchant's intended capture policy before nonprod acceptance.

Refund intent, idempotency and identifier-only outbox event commit in a caller-owned transaction.
Payment locking validates the canonical successful attempt and reserves against the remaining
balance. Partial intents are supported within that balance; no new commercial refund policy is
selected. Worker inbox commits PROCESSING before provider I/O, then acknowledges only durable
results. Crash/redelivery/concurrent execution use the identical native `rf_` key and request body.

Lifecycle remains `PENDING`, `PROCESSING`, `SUBMITTED`, `INITIATION_UNCERTAIN`, `SUCCEEDED`, `FAILED`.
Timeout, 429/5xx, conflicting responses and inconclusive initiation stay uncertain/retryable.
Provider authentication failures are configuration failures, not proof of financial failure.
A processed result or validated processed webhook establishes success; pending submission does
not. Canonical refund/payment/money/receipt/notes must agree. Terminal webhook truth wins stale
worker responses; reordered contradictory terminal events are reconciled rather than overwritten.

## 13–15. Schema and validation

No schema/model changes or migrations. Existing migrations through
`0018_customer_mobile_core`, identity challenge columns, financial FKs/constraints, inbox/outbox
and idempotency records are sufficient. Only disposable local PostgreSQL/PostGIS is used.

New tests cover exact 2Factor GET paths and template encoding; opaque references; exact positive
match; generic negative/expiry-worded and ambiguous envelopes; malformed JSON; 401/403/429/5xx;
timeouts, redirect prevention, sanitized tracebacks/DEBUG logs; explicit selection; bounded timeout;
client ownership; actual PostgreSQL challenge privacy, verified-only session issuance and replay;
same-key concurrent send prevention; ambiguous send and post-send local failure; provider-switch
and local-expiry guards. Checkout tests cover the exact DTO, ownership, authentication, cache
headers, repeated read with unchanged financial persistence, capture authority, and eligibility,
configuration, cutoff, freeze and financial-consistency failures.

Existing comprehensive Razorpay unit/PostgreSQL tests already cover outbound requests, remote
commit/lost response, concurrent duplicate-receipt recovery, canonical correlations, HMAC, event
replay/order, late capture, transaction rollback, refunds, native idempotency, overlapping worker
delivery, crash/redelivery and webhook/worker races. They are retained in the complete regression.

Executed with `.venv\Scripts\python.exe`:

| Check | Result |
| --- | --- |
| `-m ruff check .` | Passed |
| `-m ruff format --check .` | Passed |
| `-m mypy` | Passed, 139 source files |
| Focused `pytest` for `test_twofactor.py`, `test_otp_runtime.py`, `test_razorpay.py` | 139 passed in 11.96s |
| New PostgreSQL `test_twofactor_authentication.py` and `test_checkout_handoff.py` | 17 passed in 24.86s |
| `-m pytest -m 'not integration' -q --tb=short` without database URL | 432 passed, 13 skipped, 423 deselected, 4 warnings in 29.29s |
| Complete `-m pytest -q --tb=short` with disposable PostgreSQL URL | **867 passed, 1 skipped, 4 warnings in 529.94s** |
| `git diff --check` | Passed |

48 regression scenarios were added: 31 unit scenarios and 17 PostgreSQL scenarios. The complete
run collected 868 tests and exercised all database scenarios. 423 are marked `integration`; 12
existing cancellation/refund database tests lack that marker and therefore also appear in the
nonintegration selector. Those 12 were skipped in the selector run without database configuration
but **passed in the complete database-enabled run**. The sole full-suite skip is the pre-existing
POSIX exit-signal test on Windows (`tests/unit/test_deployment_job.py:99`). All four warnings are
existing Firebase `MulticastMessage.tokens` deprecations in dispatch tests. There are no new test
failures, unavailable automated checks or schema changes requiring an additional migration.

## 16. Documentation sources and unresolved vendor behavior

Primary sources inspected on the verification date:

- [2Factor AUTOGEN and VERIFY overview](https://dial2verify.com/corp/support-system/tkt/knowledgebase.php?article=19): coherent legacy paths and positive response envelopes.
- [2Factor custom template approval](https://dial2verify.com/corp/support-system/tkt/knowledgebase.php?article=21): AUTOGEN/template path after account approval. This legacy Solv Technologies article has an inconsistent “Manual” label beside AUTOGEN; do not infer a different API from the label.
- [2Factor current manual SMS samples](https://2factor.in/API/DOCS/SMS_OTP.html): POST manual-code samples, not documentation of a complete newer AUTOGEN/VERIFY pair.
- [2Factor newer offering](https://2factor.in/v4/): header API advertising does not establish compatibility with the legacy session verifier.
- [Razorpay Orders](https://razorpay.com/docs/api/orders/), [create](https://razorpay.com/docs/api/orders/create/), [fetch all](https://razorpay.com/docs/api/orders/fetch-all/): Basic auth, subunit amounts, receipts, duplicate handling and receipt filter.
- [Razorpay Payments](https://razorpay.com/docs/api/payments/), [Refunds](https://razorpay.com/docs/api/refunds/), [normal refund idempotency](https://razorpay.com/docs/api/refunds/normal-refunds-idempotent/): native refund endpoint/key/body and concurrent retry handling.
- [Webhooks overview](https://razorpay.com/docs/webhooks/), [validation/replay/order](https://razorpay.com/docs/webhooks/validate-test/), [payment events](https://razorpay.com/docs/webhooks/payments/), [refund events](https://razorpay.com/docs/webhooks/refunds/): raw-body signatures, unique event IDs and unordered/duplicate delivery.
- [React Native prerequisites](https://razorpay.com/docs/payments/payment-gateway/react-native-integration/standard/) and [Android checkout steps/options](https://razorpay.com/docs/payments/payment-gateway/react-native-integration/standard/integration-steps-android): server-created order and key/amount/currency/name handoff.

The legacy 2Factor overview gives endpoints/positive samples but does not explicitly settle
every HTTP method, status/error shape, expiry, attempt count or rate-limit policy. GET is the
explicitly approved task contract. Confirm it for this merchant account, including E.164 path
encoding and returned reference format. No exact vendor expiry/error taxonomy is assumed.
No mixed newer send/legacy verify flow is implemented. A future adapter change requires a vendor
documented complete send/verify pair and new contract tests.

Razorpay receipt filtering does not promise immediate visibility; the code does not use one empty
lookup to conclude an earlier write failed. Confirm same-receipt rejection and refund replay in
test mode. Existing runtime supports one webhook secret; official docs require the old secret for
old-event retries after rotation. Plan rotation/draining/replay deliberately; do not rotate while
old deliveries remain without a separately reviewed retained-secret strategy. Merchant account
switching with outstanding orders/refunds also requires operational reconciliation; historical
account identity/key rotation is not newly modelled in this task.

## 17. Controlled nonprod acceptance checklist

2Factor prerequisites: activated account, safely injected API key, vendor confirmation of the
legacy pair, authorized test recipient, sufficient approved test credits, and confirmed sender,
DLT entity/template linkage and template approval for the exact account. Confirm whether the
default no-template path is permitted; otherwise configure only the approved template. Obtain
written expiry/attempt/rate-limit/error semantics before aligning local intent TTL. These are
prerequisites, not claims that this repository validates account/DLT approval.

With credentials ready, perform one deliberate OTP send, matched verify, wrong-code verify,
provider expiry, local expiry, rate-limit/configuration failure where controllable, same-key
replay and provider-switch checks. Keep secrets and request paths out of logs/support exports.
Check real send latency and response format. New command ID means a deliberate new send.

Razorpay prerequisites: separate test-mode key ID/secret and webhook secret injected through the
existing secret path; explicit merchant display name; configured timeout and planning values;
approved public nonprod HTTPS webhook URL with raw bytes preserved; subscriptions for the five
handled events; intended automatic capture settings; existing refund outbox/worker wiring.
Do not use real instrument data or production credentials.

In controlled test mode: create and recover one stable-receipt order; inspect checkout handoff;
use a test native harness (frontend capability remains disabled here); confirm capture webhook,
duplicate delivery, failed payment, reordered failure after success, late capture reconciliation;
create a durable refund intent through the approved backend operation/harness and run the existing
worker; verify native same-key refund replay, pending/processed/failed notifications and timeout
reconciliation. Test native callbacks as refresh signals, never capture authority. Record safe
outcome classes/durations and identifier-only evidence. These provider tests have **not** run.

## 18. Dedicated Batch B cancellation compensation

The exact future integration point is `cancel_collection_request_by_customer` in
`src/tirodhan/modules/collection_requests/cancellation.py`: after the locked request passes
ownership, cutoff, ACCEPTED and planning checks, and before cancellation idempotency completes.
Within the same caller-owned transaction, create the approved cancellation refund intent using
`payments.refunds.create_refund(session, ...)`, the canonical successful PaymentAttempt, an
approved refund amount/reason, stable cancellation-derived business key and requesting user.
The CANCELLED transition, Refund, RefundRequested outbox event and command result must commit or
roll back together. No HTTP refund call belongs inside this transaction.

Batch B must establish a compatible global lock order: payment event processing locks Payment
before the work-unit/request, whereas cancellation currently starts with the work-unit/request.
Simply calling `create_refund` after those locks could introduce a deadlock. Review orchestration
and replay/domain uniqueness together, including different cancellation command IDs, refundable
balance, planning/capture races and crash/rollback. Approve the commercial full/partial refund
policy there. Existing already-CANCELLED replay cannot silently backfill compensation in this
provider task. No cancellation change or client refund command is introduced;
`cancellationCompensation` remains disabled.

## 19–20. Scope and credentials

Frontend and infrastructure remain unchanged. No Terraform, deployment, geocoding, H3, scheduling,
compaction, dispatch, catalogue, notification or profile capability changes. No paid service,
provider SDK or infrastructure dependency was added. No real credentials are present in this
commit; test strings are synthetic, `.env.example` contains only placeholders, and private
configuration continues to use `SecretStr` plus the existing Key Vault injection architecture.
