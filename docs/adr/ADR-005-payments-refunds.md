# ADR-005: Payments and Refunds

**Status:** Accepted; Razorpay selected as MVP production provider (Phase 1V)

## Decision

Create the collection request before payment with state `PENDING_PAYMENT`.

Serviceability must be resolved before initiating payment.

Each CollectionRequest has one logical Payment obligation.

Gateway/provider retries are separate append/history PaymentAttempts under that logical Payment.

Payment initiation is synchronous/customer-driven; the provider-hosted flow may redirect the user.

Provider webhook and/or server-to-server reconciliation is authoritative. Frontend return/timeout is UX only.

Provider events are durably deduplicated by provider event identity.

A successful attempt, logical Payment success, request transition `PENDING_PAYMENT -> ACCEPTED`, and required outbox event are persisted atomically.

`PENDING_PAYMENT` has a configurable retry expiry, then becomes `EXPIRED`; physical deletion occurs only after a separate retention/reconciliation period.

Refund is a separate financial entity/process raised against the actual successful provider charge/reference. Refund status is independent from CollectionRequest state.

The schema permits multiple/partial refunds even if MVP normally performs full refunds.

## Duplicate external success

Asynchronous payment races may cause more than one provider attempt to report success.

The system records the external truth. Only one successful attempt satisfies the logical Tirodhan Payment.

Any additional successful charge becomes a reconciliation/refund condition. It must not:

- accept the collection request again;
- create duplicate downstream events;
- be hidden merely to satisfy a DB uniqueness assumption.

## Idempotency/reconciliation

- payment/refund provider calls use stable provider idempotency keys where supported;
- payment/refund webhooks are idempotent;
- uncertain external outcomes are reconciled rather than blindly replayed;
- concurrent refund creation must not allow total committed refunds to exceed the original successful payment;
- payment instrument credentials are never stored.

## Open

- exact retry/retention timings.

### Refund Concurrency and Lifecycle Decisions
- Expiration concurrency aligns through `Payment` locking first to correctly serialize financial ownership rules against webhook success markers.
- The MVP production Payment and Refund provider is Razorpay; generic financial entities remain unchanged.
- Customer-cancellation refund policies (i.e. partial / exact commercial outcomes on cancellation) remain functionally open.
- Over-refunding concurrency operates through checking explicitly unfailed refunds to prevent race-condition payouts.
- Processing anomalies in external integrations fallback to `INITIATION_UNCERTAIN` for reconciliation instead of silent retries to guarantee provider replay-safety.

## Phase 1V provider contract

`RazorpayProvider` implements the existing PaymentProvider and RefundProvider ports with HTTPX,
HTTP Basic authentication, finite bounded timeouts, and transport retries disabled. Owned clients
are created only for complete configuration and closed on shutdown. No provider call occurs during
startup or inside a PostgreSQL transaction. Secrets arrive through deployment secrets / Key Vault
references; application code does not call Key Vault.

Payment initiation creates an Order using persisted minor-unit amount/currency, `partial_payment=false`,
the 35-character `ta_<attempt UUID hex>` receipt, and an internal attempt-ID note only. The response
must match entity, reference, receipt, amount and currency. Order IDs use existing provider columns.
Order receipt uniqueness is not treated as successful-response recovery. No undocumented Orders
idempotency header is sent. Only a command's creation owner may issue the Order POST; an uncertain
attempt or replay of an interrupted CREATED attempt is returned for reconciliation without another
POST. A concurrent replay can conservatively report uncertainty while the original call finishes.
Its known result can still be durably recorded. Local command and provider execution are not a
distributed transaction; a crash after claim commit may require reconciliation even before sending.

Razorpay Dashboard **auto-capture for Orders must be enabled** by Operations. Application code does
not configure or claim to enforce Dashboard settings. Only `payment.captured` / `order.paid` with
captured payment state and matching stored Order, amount and currency can satisfy the logical payment.
`payment.authorized` is not success. Existing canonical-success, additional-charge, payment-window,
planning-cutoff and frozen-work-unit rules remain unchanged. The frontend Checkout confirmation
verifies the stored Order-based HMAC and can associate the payment reference, but never marks
Payment SUCCEEDED, accepts a booking or emits an acceptance event.

Webhook HMAC-SHA256 validates the exact raw bytes before JSON parsing using the independent webhook
secret. Deduplication uses the documented `x-razorpay-event-id` header; if absent/invalid, the raw-body
SHA-256 hex digest provides exact-redelivery identity. Authenticated unsupported/malformed events
are recorded UNMATCHED, never interpreted as success. Body/signature/financial credentials and
arbitrary error text are neither logged nor stored. Order identity can correlate an event without
an internal note; conflicting note/provider references become RECONCILIATION_REQUIRED.

Refund POST targets the original canonical provider payment reference and always sends the exact
positive minor-unit amount, normal speed and an internal refund-ID note. Razorpay's documented
Payment Gateway `X-Refund-Idempotency` header uses SHA-256 hex of the existing stable internal key
(whose ':' is outside Razorpay's allowed alphabet). No RazorpayX header is invented. Responses
validate refund entity/reference, payment, amount and currency; `processed`, `pending`, `failed`
map to SUCCEEDED, SUBMITTED, FAILED. `refund.created`, `refund.processed`, `refund.failed` reconcile
via existing generic columns and validate identity/financial facts without copying provider payload.

RefundRequested is the only new publisher allow-list entry, routed to a dedicated Service Bus queue
with `{refund_id}` only. The Peek-Lock worker renews broker locks, commits inbox PROCESSING before
execution and PROCESSED only after a durable outcome. PENDING resumes safely; interrupted PROCESSING
becomes INITIATION_UNCERTAIN. Uncertain refunds are not automatically POSTed again, even though the
adapter supplies native idempotency as additional protection. A still-running original invocation may
persist a known result after conservative uncertainty, without overwriting a terminal webhook result.
Exactly-once external execution is not claimed. No financial reservation is released by uncertainty.

### Official references checked for Phase 1V

- [Create Order](https://razorpay.com/docs/api/orders/create/)
- [Standard Checkout verification](https://razorpay.com/docs/server-integration/python/test-app/)
- [Webhook validation and event identity](https://razorpay.com/docs/webhooks/validate-test/)
- [Payment webhook payloads](https://razorpay.com/docs/webhooks/payments)
- [Normal Refund](https://razorpay.com/docs/api/refunds/create-normal/)
- [Payment Gateway refund idempotency](https://razorpay.com/docs/api/refunds/normal-refunds-idempotent)
- [Refund webhook payloads](https://razorpay.com/docs/webhooks/refunds)

Commercial cancellation-refund policy, reconciliation automation and retention values remain open.
