# Tirodhan — Payment Reconciliation, Payment Uncertainty, and Event-Driven Customer Updates

**Status:** Architecture decisions agreed in discussion; implementation remains subject to source review and explicit PR approval.

**Scope:** Customer Mobile, Operations Web, FastAPI financial domain, payment provider integration, existing transactional outbox, notification delivery.

**Provider:** Razorpay (test environment for nonproduction).

**Architecture:** FastAPI modular monolith, PostgreSQL authoritative state, Azure Container Apps workers/jobs, existing outbox/inbox, FCM/APNs mobile push. No additional realtime transport required.

## 1. Product principles and boundaries

1. **Payment evidence, not checkout time, determines success or failure.** A five-minute frontend countdown is a UX boundary. It is not evidence of failed collection or an unsuccessful charge.
2. **Do not permit online repayment while an earlier charge remains unresolved.** An explicit, authoritative failure may enable a new payment attempt, subject to current booking eligibility.
3. **Do not introduce Cash on Delivery or Pay at Pickup** as a fallback for delayed provider confirmation. Payment-before-acceptance remains authoritative.
4. **An unresolved booking remains `PENDING_PAYMENT`, not `ACCEPTED`.** It must not enter planning, dispatch or fulfillment as a paid booking.
5. **Every verified financial event remains auditable**, including events arriving after a customer's screen has closed, after booking expiry, or after a later transition.
6. **Manager reconciliation verifies evidence**; it does not blindly edit payment or collection statuses.
7. **Backend financial state is authoritative**. Mobile push is an invalidation/navigation hint, not permission to mark a charge captured.
8. **Expo Go is temporary screen-preview tooling.** It does not constrain the actual React Native/Expo + FCM/APNs production architecture.

## 2. Distinct concepts and clocks

| Concept | Meaning | Consequence |
|---|---|---|
| Checkout UX countdown | Example: five minutes to remain on provider checkout/status UI | Exit provider screen or show Activity; **does not** imply failure |
| Payment attempt state | Definitive failure, awaiting provider outcome, or authenticated successful capture | Governs whether online retry is allowed |
| Collection payment state | Whether the logical booking's financial obligation is satisfied | Governs acceptance eligibility |
| Pickup-slot cutoff / freeze | Existing scheduling policy | A late capture must not force acceptance into an unserviceable slot |
| Reconciliation schedule | Frequency at which the backend consults provider APIs | Independent of mobile countdown and webhook timing |
| Financial dispute/operations queue | Long-outstanding or anomalous charge status | Does not discard payment evidence or silently manufacture an outcome |

No finite timeout, including five minutes, can itself distinguish a delayed capture from a failed charge.

## 3. Customer-visible outcomes

| Verified backend outcome | Collection status | Activity presentation | Allowed action |
|---|---|---|---|
| Attempt pending/uncertain; provider outcome not verified | `PENDING_PAYMENT` | **Pending payment**; explanatory text that verification is ongoing | Refresh/status details; **no additional online payment** |
| Provider confirms definitive failure and booking remains eligible | `PENDING_PAYMENT` | **Payment failed** | Retry payment |
| Provider verifies capture; booking still eligible | `ACCEPTED` | **Payment received** (avoid ambiguous “Received”) | View booking; no second charge |
| Provider verifies capture but slot expired/frozen or booking otherwise ineligible | Do not force `ACCEPTED` | **Payment received — pickup requires resolution** or equivalent distinct state derived from backend | Operations resolution / refund or approved rebooking |
| Booking expires with earlier payment attempt still unresolved | No automatic false `FAILED` from elapsed time | **Payment verification pending** with booking expiry/assistance messaging, per explicitly approved state model | No immediate repay until financially safe |
| Duplicate provider notification | No second effect | No misleading second transition | None |

Customer-facing wording and DTO changes require frontend contract alignment. The table states the desired UX, **not a claim that all enum values or projections already exist**.

## 4. Normal online payment journey

1. Customer confirms booking details and backend creates a durable logical Payment/PaymentAttempt and Razorpay order under existing idempotency.
2. Customer Mobile opens native Razorpay checkout. The UI may show a five-minute countdown as a waiting affordance.
3. Razorpay checkout callback is **not** authoritative evidence of capture; it only triggers status refresh or navigation.
4. Razorpay webhook, automated status inquiry or manager-triggered status inquiry verifies outcome and enters the shared canonical reconciliation service.
5. A verified eligible capture changes financial state and collection state atomically according to existing acceptance rules; all required domain/outbox events are recorded.
6. A verified failed attempt enables another online attempt only after backend eligibility, active cutoff/horizon and unsettled-attempt checks.
7. If the checkout UI times out, closes, or the customer navigates back without verified outcome, Activity shows **Pending payment**, and retry stays blocked.
8. If a payment subsequently arrives, the backend reconciles it regardless of app state; the next notification-triggered or lifecycle-triggered read shows the authoritative outcome.

**Do not hold a five-minute FastAPI HTTP request open** waiting for Razorpay; the checkout UI countdown and server-side asynchronous confirmation are separate.

## 5. Automated Razorpay reconciliation

### 5.1 Trigger and inputs

- A scheduled Azure Container Apps Job or existing appropriately provisioned worker periodically selects unresolved eligible payment attempts from PostgreSQL.
- Every candidate has durable identity, provider order ID when known, previous check status/time, and appropriate retry/backoff metadata.
- Use read-only Razorpay **payment and order/payment-list inquiries** where supported; creation of an order is not proof of capture.
- Query only accounts/merchant configuration corresponding to the original attempt. Never allow credentials for a new account to silently rewrite historical payment identity.
- Classify returned results as verified capture, definitive failure, still pending/unknown, inconsistent or provider unavailable.
- Rate-limit, back off, bound batch sizes, and avoid concurrent competing queries for the same attempt. Preserve at-least-once processing and idempotent business effects.

### 5.2 Proposed configurable cadence — NOT vendor-prescribed

| Age of unresolved attempt | Suggested inquiry interval |
|---|---|
| 0–5 minutes | Normal webhook path and customer-requested refresh; no assumption of failure |
| 5–15 minutes | Every 2 minutes |
| 15–60 minutes | Every 5 minutes |
| 1–24 hours | Every 30 minutes |
| Beyond 24 hours | Operations attention plus lower-frequency provider reconciliation |

These are **proposals requiring operational approval** after Razorpay rate-limit/cost review and actual nonprod latency measurements. Store the next due time so a single coarse scheduled job can service age-based intervals without creating many platform schedules. Avoid unbounded scans and redundant provider queries.

### 5.3 Result application

Provider inquiry and provider webhook must converge on the **same canonical financial state machine**. Avoid separate status-updating implementations. Validate provider payment/order identity, merchant account, capture status, amount, currency, mapping to the correct logical booking, and duplicate events. Preserve transaction boundaries, payment-first lock order, durable event identity and outbox. An inquiry must not directly set `SUCCEEDED` using a user-supplied transaction reference alone.

## 6. Manager-initiated reconciliation

### 6.1 Operations Web behavior

Operations Web should provide a role-protected Payment Reconciliation view where a manager may locate a collection, inspect its current payment/attempt state, enter a Razorpay order/payment reference received via support (for example WhatsApp), request a fresh inquiry, and view the resulting verified state and audit trail.

The manager action is **verify-and-reconcile**, never “set payment successful.” A screenshot, customer bank debit SMS, or bank-statement line alone does not prove captured merchant-side payment. Such material may support investigation but must not bypass provider evidence or account/amount/ownership matching.

### 6.2 Security, concurrency and evidence

- MANAGER authorization and audit record: actor, timestamp, collection/attempt, submitted reference, provider lookup result classification, correlation identifiers, prior/post state, sanitized reason.
- Rate-limit manager inquiries; prevent transaction enumeration or exposing customer PII.
- Verify at Razorpay using server-side credentials; require exact merchant identity, order/payment relation, currency, amount, confirmed capture and unique provider charge ID.
- Use the same lock ordering/idempotent state transition and outbox path as automatic reconciliation/webhooks.
- No manager edit of the amount, provider references or success state via raw CRUD.
- Existing scheduling cutoff/freeze still applies. Financial capture may be verified even if pickup acceptance cannot occur; route to operational resolution rather than bypassing planning.
- Any exceptional *bank-only attestation* without provider verification is **not approved for V1**. It would require a separate policy and stronger dual controls.

## 7. Late capture, expiry and additional charges

1. A late verified capture must never be discarded solely because the user navigated away or a checkout timer elapsed.
2. If the collection remains eligible, process the canonical capture and acceptance using established invariants.
3. If the slot is expired/frozen/cancelled, record the money as captured without falsely accepting the collection. Preserve and surface a refund/rebooking/operations obligation under separately approved policy.
4. While an attempt is unresolved, **do not offer a second online payment**. After an authoritative, definitive failure, allow a new attempt only if booking eligibility still holds.
5. Should multiple distinct captures nevertheless occur (provider retries, race, historical events, or anomalies), every charge requires **per-charge accounting and reconciliation**. The current canonical-only refund cap is not a complete solution; automatic additional-charge refund is a separately tracked implementation gap. Do not conflate repeat webhook delivery for the same charge with a second actual charge.
6. The existing payment-expiry job must be reviewed before deployment: it must not turn an unresolved provider charge into a falsely definitive failure or prevent recording/reconciling a late payment. Preserve separation between booking lifecycle and financial uncertainty.

## 8. Event-driven status updates to native mobile UI

### 8.1 Delivery model

**Do not use continuous polling or persistent WebSockets/SSE for ordinary Activity status changes.** A correctly committed backend state change creates a durable event; existing outbox/inbox-driven notification processing sends a minimal mobile push through FCM and APNs. The mobile app receives a signal and **refetches** authoritative collection/payment state from FastAPI. Push content never itself drives acceptance or financial truth.

```text
Razorpay webhook / scheduled inquiry / manager inquiry
                  |
                  v
   FastAPI shared reconciliation transaction
        PostgreSQL Payment/Attempt/Collection
                  + transactional outbox
                  |
                  v
     Existing event publisher / notification worker
                  |
                  v
              FCM -> APNs
                  |
                  v
  Customer Mobile push handler -> query invalidation
                  |
                  v
     GET authoritative payment / collection details
                  |
                  v
          Activity renders latest state
```

### 8.2 What the app does

| App state | Expected behavior |
|---|---|
| Foreground, Activity visible | Receive supported notification signal, invalidate relevant React Query keys and fetch latest state |
| Foreground, other tab | Invalidate targeted financial/collection queries; refetch when screen becomes relevant |
| Background/closed | Show allowed notification if delivered; tapping opens related Activity/detail, then fetches backend truth |
| Notification delayed/missed/disabled | Refresh on app resume, opening Activity/detail, or explicit manual refresh |
| Offline | Preserve last clearly labelled cached state; refresh on restored connectivity without inventing payment confirmation |

Push is **best effort**, not an exact-once realtime guarantee. A notification may arrive late, be duplicated, be suppressed by OS permissions, or be delivered out of order. It must carry only minimal identifiers/type, no full payment details, secrets, phone, precise location or untrusted final-status authority. Build correct deduplication, navigation and query invalidation. Never expose sensitive financial evidence in notification preview text.

### 8.3 Delivery implementation obligations

- Ensure state transition + notification intent are committed consistently, using the existing outbox model rather than direct provider calls inside financial transactions.
- Reuse existing notification-registration, revocation, token rotation and per-customer ownership model.
- Confirm actual notification worker, FCM/APNs credentials, retry/backoff, dead-letter/operations review and expected delivery guarantees; don't assume they are deployed because frontend code exists.
- Support app resume/status refresh as fallback with **no continuous polling**.
- Avoid a separate status stream, long-held ACA requests, Redis or added paid realtime infrastructure.
- The ability to receive app-owned iOS notifications requires suitable signed builds/APNs entitlements. Expo Go is a temporary *screen preview* and is not an architectural limitation.

## 9. Key invariants and implementation guardrails

- Provider-authenticated webhook or server-side Razorpay inquiry = evidence source; client-side callback/user submission is not.
- Only one canonical successful charge satisfies one logical booking; each additional successful charge remains separately accountable.
- No repayment while unresolved, regardless of how long the frontend countdown has elapsed.
- Definitive provider failure permits a retry only if booking remains eligible; a *timeout* never implies definitive failure.
- Do not authorize collection acceptance after planning cutoff/freeze via manager override.
- Use Payment -> work-unit -> request lock discipline and existing idempotent event mechanisms.
- No COD/deferred-payment exception.
- Preserve financial truth if booking state expires or customer abandons UI.
- Separate payment settlement/capture evidence from eventual bank payout; bank statement appearance is not required for ordinary capture verification.
- Respect explicit frontend capability gates and nonprod verification.

## 10. Backend and frontend contracts requiring review

The existing backend Batch B branch reportedly provides `GET /v1/customer/collection-requests/{request_id}/payment`, `GET /v1/customer/collection-requests/{request_id}/refunds`, and customer detail. Its current `Payment`/Attempt and Collection states need an explicit projection rule to distinguish *attempt outcome unresolved* from *booking expired*, plus retry eligibility tied to definitive failure.

New manager reconciliation command/view and scheduled Razorpay status-inquiry port should reuse the financial application service. No new endpoint path, event schema, database column or new frontend enum is approved in this document; implementation must inventory existing contracts and propose minimum changes before editing. Activity must distinguish **Pending payment**, **Payment failed**, **Payment received**, and **Payment received — pickup requires resolution** without asserting statuses not yet supported by backend.

## 11. Tests and acceptance

- Confirmed capture through webhook → one accepted collection when eligible, correct outbox and Activity refresh.
- Same captured payment found by webhook and automated inquiry → one business effect.
- Manager inquiry finds captured payment before webhook → one effect, audited manager action.
- Unknown/still-pending inquiry → retain unresolved state, no retry, no acceptance.
- Definitive provider failure → eligible retry, no fabricated capture.
- Five-minute checkout timeout → Activity pending; no HTTP request held open and no charge-state mutation solely from timer.
- App closed/backgrounded → notification-tap navigation and subsequent authoritative refetch.
- Duplicate/out-of-order/missing push → UI remains correct after lifecycle/refetch.
- Cancelled/expired/frozen late capture → durable financial truth without invalid booking acceptance.
- Wrong merchant/amount/currency/reference → reconciliation exception, never accepted.
- Multiple genuine captures → separate accounting required; record as outstanding blocker until implemented.
- PostgreSQL lock-order, concurrent webhook/inquiry/manager invocation and crash-redelivery tests.
- Nonprod Razorpay sandbox tests and actual device push acceptance tests prior to production enablement.

## 12. Decisions and outstanding questions

**Agreed:** no COD, no online retry while provider state unknown, no acceptance while payment unresolved, definitive failure allows eligible retry, manager can trigger provider-verified reconciliation, automated scheduled reconciliation is preferred, push-triggered refetch + lifecycle refresh replaces routine polling, Expo Go is temporary.

**To approve before code:** reconciliation cadence/provider limits, booking expiry policy during unresolved payment, customer-visible statuses and support messaging, manager audit/access details, exceptional financial resolution for paid-but-unserviceable slots, separate additional-charge accounting, failed-refund operational recovery, legacy cancellation remediation, push worker/runtime readiness.

## 13. Documentation placement and change control

Recommended canonical document in backend repo:

`docs/PAYMENT_RECONCILIATION_AND_MOBILE_STATUS_EVENTS.md`

Cross-reference it from `docs/adr/ADR-005-payments-refunds.md`, `docs/DOMAIN_MODEL.md`, and `docs/IDEMPOTENCY.md`; record necessary implementation gaps in `docs/PHASE_2_BATCH_B_REPORT.md` without rewriting past test results. Frontend repo should link the backend document from `apps/customer-mobile/docs/CUSTOMER_MOBILE_BACKEND_CONTRACTS.md` and document Activity notification-driven refresh in its architecture notes. The backend and frontend remain separate repos; avoid cross-repo code changes in the documentation-only phase. Do not claim that the document alone implements scheduled reconciliation or notification delivery.

**Implementation ownership:** Future focused backend branch for reconciliation and financial exceptions; separate frontend branch for Activity UX and push response where needed. Review/PR/merge separately. No unapproved new infrastructure.


## Repository implementation references

The approved policy above is preserved. Implementation mechanisms and validation are
recorded in [the completion report](PHASE_2_FINANCIAL_RECONCILIATION_COMPLETION_REPORT.md).
See [ADR-005](adr/ADR-005-payments-refunds.md), [domain model](DOMAIN_MODEL.md),
[schema](SCHEMA_DESIGN.md), and [idempotency](IDEMPOTENCY.md). Proposed polling
intervals are not production defaults; normal-refund failure finality requires
provider/account confirmation before replacement can be authorized.
