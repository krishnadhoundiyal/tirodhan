# Tirodhan — Historical and Late-Payment Financial Exceptions Policy

**Status:** Approved manager-in-the-loop policy; implementation and historical audit pending

**Scope:** Late captured payments on unfulfillable bookings, legacy cancellations without refund intents, and unresolved historical financial exceptions

**Suggested backend repository path:** `docs/HISTORICAL_FINANCIAL_EXCEPTIONS_POLICY.md`

**Related:** `docs/adr/ADR-005-payments-refunds.md`, `docs/PHASE_2_BATCH_B_REPORT.md`, payment reconciliation and mobile status events specification, failed-refund recovery policy

## 1. Executive decision

**Manager intervention is required when a verified late payment cannot safely yield the originally scheduled pickup, or historical records do not prove that money owed to a customer was refunded.** Tirodhan must retain financial evidence and the unresolved obligation until an authorized resolution is completed. Managers do not directly edit `Payment.status`, `CollectionRequest.status`, or `Refund.status`.

This is distinct from the normal eligible-booking case: a Razorpay-verified capture received in time and satisfying current planning rules follows the existing automatic acceptance path without manager approval.

## 2. Quick-reference decision table

| Case | Automatic system behavior | Manager in loop? | Permitted resolution |
|---|---|---|---|
| Payment pending; Razorpay outcome unknown | Keep `PENDING_PAYMENT`; no second online payment; reconcile by webhook and read-only inquiry | Optional investigation | Await verified success/failure |
| Definitive payment failure, booking still eligible | Record failure; allow controlled online retry | No | New attempt under existing idempotency/expiry rules |
| Verified capture, valid unexpired/unfrozen booking | Accept if all authoritative invariants hold | No | `ACCEPTED`, customer sees “Payment received” |
| Verified capture **after expiry** | Record captured money; **do not** accept expired booking | **Yes** | Manager reviews and authorizes appropriate financial resolution; normally refund |
| Verified capture after **planning cutoff/freeze** where booking cannot be serviced | Record money; do not insert into frozen planning population | **Yes** | Manager reviews/authorizes refund or separately approved operational remedy |
| Already `CANCELLED`, verified payment captured late | Preserve `CANCELLED`; preserve obligation and reconcile | If ordinary approved cancellation compensation already applies, use that rule; otherwise **yes** | Existing atomic compensation or audited exception resolution; never reopen silently |
| Legacy `CANCELLED` with captured payment but **no refund intent** | Detect gap; do not assume refunded or silently backfill on customer replay | **Yes** | Investigate history; authorize single durable recovery intent if owed |
| Refund definitively failed | Preserve open liability | **Yes** | Authorize replacement only after provider-verified definitive failure (see companion policy) |
| Historical unknown/duplicate charges | Preserve each authenticated charge and correlation | **Yes** where unresolved | Reconcile charge-specific liability; do not mark resolved without evidence |
| `OPERATIONS_ADJUSTMENT` reason not representable in customer DTO | Fail safely rather than mislabel | **Yes** for contract/policy resolution | Approve accurate public reason; no fabricated correction |

**No COD / Pay at Pickup fallback** is introduced. An unresolved online payment does not cause collection acceptance. The five-minute checkout countdown is UX only, not a payment-failure or booking-acceptance proof.

## 3. Operations Financial Exceptions queue

A minimal PostgreSQL-backed case record or well-justified durable projection should capture:

| Information | Purpose |
|---|---|
| Exception ID, category and state | Stable triage and recovery identity |
| Collection ID, logical payment ID | Business linkage and ownership |
| Razorpay order/payment/refund identifiers | Evidence linkage; provider verification |
| Captured amount and currency | Exact liability assessment in minor units |
| Slot, cutoff, freeze and request state | Why normal acceptance is unsafe |
| First detected, last checked, next check | Aging, escalation, reconciliation cadence |
| Evidence source and safe reference | Webhook/poll/manager-verified evidence, without sensitive raw exports |
| Proposed resolution and authorized amount | Transparent manager decision |
| Manager identity, timestamp, reason | Auditable authorization |
| Resolution operation/outbox identity | Exactly-once business effect and traceability |

Cases must be queryable by customer complaint reference and collection ID, without exposing one customer's financial details to another.

## 4. Late captured payment flow

1. Razorpay webhook, scheduled reconciliation or manager-triggered lookup yields a **verified captured payment**.
2. Backend persists the provider payment identity, amount, currency and authoritative mapping to an existing collection/payment. Deduplicate provider events and distinguish different actual charges.
3. Under established payment-first and planning work-unit lock order, check original booking eligibility, expiry, cutoff, freeze and cancellation.
4. **If currently eligible:** follow existing canonical acceptance logic and emit the normal status event.
5. **If unfulfillable:** retain the captured-funds financial truth; do **not** set `ACCEPTED`, reopen expired/cancelled requests, or modify immutable planning batches.
6. Create or upsert one durable financial exception per distinct underlying liability, with safe evidence and a clear manager work item. Do not manufacture a provider refund or claim completion.
7. Manager initiates fresh read-only provider verification as needed and chooses an authorized resolution. Normal expectation: refund verified funds that cannot be legitimately retained. Any proposed rescheduling is a **separate commercial/domain design decision**, not an implicit status edit.
8. Approved refund resolution atomically creates its durable obligation/intent and outbox; existing refund worker interacts with Razorpay after commit.
9. Push an identifier-only change signal where supported; customer frontend refetches canonical state. Display “Payment received — pickup requires review” rather than falsely claiming an accepted pickup.
10. Keep the exception open until the financial obligation is verified resolved. Manager approval alone does not equal provider refund success.

## 5. Legacy cancellation without refund intent

1. Identify historical `CANCELLED` requests and their confirmed captured charges, canonical and additional.
2. Query recorded refunds, provider operations, events and reconciliation evidence to identify whether any amount has already been repaid or remains uncertain.
3. Do **not** infer missing refunds solely from a cancellation status, and do **not** backfill in an ordinary cancellation command replay.
4. Create an auditable exception for any unexplained unpaid obligation; ensure repeated scans do not generate duplicate cases or refund commands.
5. Manager reviews and verifies payment and refund truth with Razorpay (and approved settlement evidence when required).
6. If money is owed and no competing provider operation remains payable, authorize the smallest correct refund obligation and the corresponding durable outbox event.
7. Retain original historical records; append corrective actions/audit instead of rewriting history.
8. Resolve only after verified provider outcome and balance reconciliation.

## 6. Manager controls and guardrails

| Action | Allowed? | Preconditions |
|---|---|---|
| View exception/evidence timeline | Yes | Authorized MANAGER with limited necessary data |
| Trigger Razorpay status inquiry | Yes | Authorized manager; rate-limited and audited |
| Confirm verified financial evidence | Yes | Backend independently validates origin, merchant, amount, currency, identity and status |
| Authorize a justified refund intent | Yes | Liability established, no duplicate payable refund, policy permits, idempotent audited command |
| Manually set Payment to `SUCCEEDED` | **No** | Must use common verified reconciliation pipeline |
| Manually set collection to `ACCEPTED` after expiry/freeze | **No** | Cannot override planning/booking invariants |
| Mark refund `SUCCEEDED` without provider evidence | **No** | A manager approval is not payment confirmation |
| Delete/overwrite historical charge evidence | **No** | Append-only or otherwise tamper-evident audit |

## 7. Shared technical implementation constraints

- **One financial truth path:** webhook, scheduled inquiry and manager-triggered inquiry must converge on the same idempotent reconciliation service.
- **Transaction order:** preserve payment-before-work-unit/request locking; do not reintroduce capture/cancellation/planning deadlocks.
- **Charge-specific identity:** different successful Razorpay charges are distinct obligations; repeated webhook deliveries for the *same* charge are not.
- **Outbox:** manager-approved financial intent and its outbox event commit together; no external Razorpay mutation during a DB transaction.
- **No silent acceptance:** unserviceable paid bookings are recorded as financially confirmed but operationally unresolved.
- **No silent expiry on uncertainty:** five-minute UX timer does not prove payment failed. The expiry worker and pending-payment policy require explicit alignment before release.
- **Customer UI:** Activity exposes true pending, failed, accepted, and “paid but requires review” states; push triggers API refetch, not local status mutation. Push is not required to be immediate or reliable; app-foreground refresh is fallback.
- **Security:** no direct customer financial override; audit all manager decisions; don't log raw financial credentials or sensitive WhatsApp screenshots.
- **Infrastructure:** existing FastAPI modular monolith, PostgreSQL, Azure Container Apps scheduled jobs/worker and Razorpay interfaces; no Redis/WebSockets/SSE required.

## 8. Exception aging and operations

Configure automated reconciliation cadence, age thresholds, escalation and manager SLAs after validating provider behavior and throughput. An old exception must remain visible and financially accountable; **aging never converts an uncertain transaction into a failed one**, never silently forgives money owed, and never authorizes a new charge/refund operation.

## 9. Acceptance scenarios

| Test case | Required result |
|---|---|
| Capture received after slot expiry | Money recorded; booking not accepted; manager exception created |
| Capture received after planning freeze | Frozen batch unchanged; manager exception created |
| Eligible timely capture | Automatic acceptance, no unnecessary exception |
| Duplicate webhook + scheduled inquiry | One captured charge, one exception/acceptance effect |
| Manager and scheduler reconcile same charge concurrently | Same authoritative result, no conflicting transitions |
| Two different charge IDs for one booking | Both recorded independently; no lost additional-charge liability |
| Legacy cancelled request with no refund intent | Audited case; no duplicate refund on repeated scan/replay |
| Legacy cancelled request with existing uncertain refund | No replacement; original operation reconciled |
| Manager authorizes recovery twice | One approved financial effect; idempotent response/conflict |
| Provider unavailable during manager inquiry | No manual success assertion; case remains open |
| Refund approved but worker fails | Exception/obligation stays open; no false “refund completed” |
| Push delayed or dropped | Foreground/activity API refresh shows authoritative truth |

## 10. Implementation delta and unresolved decisions

**Needed:** manager exception queue and authorized reconciliation operations; scheduled read-only provider inquiry; charge-level accounting and reconciliation for additional captures; audited manager approval to create refund obligations; historical audit/backfill procedure; suitable public DTO states/reason mapping and UI; reconciliation event-to-push wiring; tests, migration and nonprod acceptance.

**Not decided by this document:** commercial rescheduling instead of refund for unfulfillable slots; exact automated polling interval/provider limits; operator staffing/SLA; ambiguous settlement evidence outside provider APIs; partial balance policy; frontend enum extensions; historical batch-repair authorization and deployment order. Those require explicit decisions rather than Codex assumptions.

**Hard restriction:** Do not enable unrestricted cancellation compensation or manager overrides before financial invariants and nonprod tests are demonstrated.


## Repository implementation references

The approved policy above is preserved. Implementation mechanisms and validation are
recorded in [the completion report](PHASE_2_FINANCIAL_RECONCILIATION_COMPLETION_REPORT.md).
See [ADR-005](adr/ADR-005-payments-refunds.md), [domain model](DOMAIN_MODEL.md),
[schema](SCHEMA_DESIGN.md), and [idempotency](IDEMPOTENCY.md). Proposed polling
intervals are not production defaults; normal-refund failure finality requires
provider/account confirmation before replacement can be authorized.
