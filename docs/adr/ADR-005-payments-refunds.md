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
- Phase 2 Batch B approves full customer-cancellation compensation before cutoff/freeze;
  partial commercial outcomes remain open (see the policy below).
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

## Phase 2 Batch B customer cancellation policy

The Batch B implementation request approves one customer command before the existing planning
cutoff/freeze: cancel an `ACCEPTED` or still-unsettled `PENDING_PAYMENT` collection. A confirmed
canonical successful payment receives a full refund intent, subject to the authoritative balance.
An already reserved full refund is reused. A partial reservation without an approved partial policy
refuses cancellation for financial review. Definitively failed refunds do not reserve balance.

Cancellation, refund intent, `RefundRequested` and command completion commit together. No provider
HTTP occurs in this transaction. An unsettled cancellation creates no refund and closes the logical
payment obligation. If a later authenticated capture establishes its first canonical successful
charge, that event transaction persists financial success and the full cancellation refund/outbox,
while leaving the collection `CANCELLED`, including after expiry or planning freeze.

This supersedes Phase 1V's exclusion of automatic customer-cancellation refund initiation only.
Razorpay execution and refund lifecycle remain unchanged. A durable intent is not a completed refund.

The existing canonical-attempt-only refund model and logical-payment-wide amount cap remain
authoritative. A separate additional charge cannot be automatically refunded within those rules.
Its authenticated charge reference, amount and currency are retained on the provider event, which
remains `RECONCILIATION_REQUIRED`; no extra acceptance or fabricated refund is permitted. Approving
per-charge accounting is a separate architectural decision. Existing non-cancelled expiry/cutoff
captures remain in their established reconciliation path; this policy does not authorize their
commercial disposition. Legacy cancelled records without compensation require an audited recovery
decision; ordinary command replay must not silently rewrite history.


## Accepted Phase 2 reconciliation extension

The current user-approved business policies are incorporated at:
[reconciliation/mobile events](../PAYMENT_RECONCILIATION_AND_MOBILE_STATUS_EVENTS.md),
[failed refund recovery](../FAILED_REFUND_RECOVERY_POLICY.md), and
[historical exceptions](../HISTORICAL_FINANCIAL_EXCEPTIONS_POLICY.md).
They supersede the historical canonical-only combined refund cap and the previously open
late/historical recovery decisions in this ADR. Full compensation is per actual verified
captured charge, preserving one logical Payment and the canonical booking acceptance.

Existing Refund represents provider execution; RefundObligation preserves debt independent
of a failed execution. Manager replacement requires current non-payable API proof and
complete charge/balance accounting, with new operation/native idempotency identity.
Missing/ambiguous evidence fails closed. Exceptional unfulfillable late captures require a
manager decision; ordinary authorized cancellation and extra-charge compensation remain
automatic. Historical transactions are not guessed or silently backfilled. Scheduling values
and account-specific normal-refund failure finality still require operational/provider
confirmation, not inferred production defaults. See the completion report for implementation,
compatibility, validation and remaining deployment prerequisites.

## Accepted financial correctness closure — 2026-10-10

The current closure request supersedes the historical Phase 1V exclusions only where needed
for queued ingress and independent financial controls. Exact-byte HMAC/account validation now
precedes awaited Service Bus acceptance and HTTP acknowledgement, rather than synchronous
financial DB mutation. A dedicated queue and separate sender/receiver workload identities feed
the existing PostgreSQL processor. The consumer transaction commits inbox/evidence/domain/outbox
before broker completion. Contradictory event-ID/hash observations are retained separately.

Razorpay Refund now persists its actual native `rf_<UUID hex>` key. Provable old neutral keys
normalize without changing the earlier wire key. Uncertain POST recovery first queries provider
truth; an absent operation permits same-body/native-key replay only inside an explicitly confirmed
retention window. Public docs do not establish this window. Default closed replay and failure-
finality gates prevent an unproved replacement. Manager commands still require current proof,
complete inventory, correct reservation and audit; no financial override is added.

Bounded finite account inventory/report controls use PostgreSQL checkpoints and typed immutable
SettlementEvidence, with existing charge/refund/case relationships. Disputes are evidence and
investigation holds, with no auto-refund/reopening. Missing settlement expectations require
verified membership, due facts and coverage. No invented `refund.reversed` event is supported.
No universal history/version architecture, ledger service or new paid provider is introduced.

Service Bus Standard and ACA Consumption remain selected. API minimum replicas stay zero;
provider five-second acknowledgement versus cold-start performance remains an operational
gate. Terraform is preparation only; account contracts, report access, live provider behavior
and real deployment are not verified here. See [closure evidence, provider questions and release classification](../PHASE_2_FINANCIAL_CORRECTNESS_CLOSURE.md).
