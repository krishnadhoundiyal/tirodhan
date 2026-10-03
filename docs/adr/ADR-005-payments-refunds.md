# ADR-005: Payments and Refunds

**Status:** Accepted; production provider Razorpay (Phase 1V)

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
- Production payment and normal refund execution use Razorpay (`RAZORPAY`).
- Customer-cancellation refund policies (i.e. partial / exact commercial outcomes on cancellation) remain functionally open.
- Over-refunding concurrency operates through checking explicitly unfailed refunds to prevent race-condition payouts.
- Processing anomalies in external integrations fallback to `INITIATION_UNCERTAIN` for reconciliation instead of silent retries to guarantee provider replay-safety.

## Phase 1V runtime decision

Use async httpx Basic Auth with bounded timeout and disabled automatic HTTP retries.
Orders use receipt `pa_<payment_attempt_id.hex>`: lookup before creation and receipt recovery
after duplicate/ambiguous creation. Never change receipt to escape uncertainty. Validate entity,
provider ID, receipt, exact amount/currency before returning an established order.
Dashboard auto-capture is required; payment.captured is authoritative, payment.authorized is not.
Raw-byte HMAC verification and required x-razorpay-event-id precede DB mutation. Persisted provider
order/payment references arbitrate correlation, not payment notes; amount/currency must equal
the locked logical Payment. Existing canonical success, planning lock/cutoff and expiry rules remain.

Normal refunds use identical bodies and native `X-Refund-Idempotency: rf_<refund_id.hex>`;
stored provider-neutral key remains `refund:<uuid>`. PROCESSING/UNCERTAIN safely resume this
same operation after crashes. Validate identity/canonical charge/amount/currency and preserve
terminal webhook truth on stale worker results. RefundRequested is identifier-only, explicitly
routed to a dedicated queue; resumable Peek-Lock inbox completes only on durable initiation
outcome SUBMITTED/SUCCEEDED/FAILED. Provider calls run without DB sessions/locks.

No manual capture, instant refunds, client-authoritative success, auto-refund, new financial schema
or deployment Terraform. Account activation, auto-capture, webhook subscriptions, queue/RBAC and
secret injection are operational prerequisites (see PHASE_1V_COMPLETION.md).
