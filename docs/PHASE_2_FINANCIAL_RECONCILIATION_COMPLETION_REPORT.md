# Phase 2 financial reconciliation completion

## Pre-implementation impact assessment

Base: `e00921c1563884eb4989d2ab2faa9bbe43f36cc3`, branch
`phase2/customer-financial-cancellation`. Tracked preflight tree is clean; the three
untracked approved policy documents are inputs. Batch A and provider integration
ancestors and migration 0019 are present.

1. Existing Payment remains the logical booking payment. PaymentAttempt remains an
   order/checkout attempt; Refund remains a provider execution operation. Provider
   events remain minimal evidence, never raw payloads.
2. Add CapturedCharge with unique `(provider, provider_payment_id)`, real payment,
   attempt and first-evidence FKs, exact amount/currency. One charge funds booking;
   additional charges never change the quote or accept the booking again.
3. Add RefundObligation per charge, with a bounded amount and blocked flag. Existing
   Refund rows become linked execution attempts; nullable links preserve old history.
   Failed execution does not remove the obligation or release reserved money unless
   independently verified as non-payable. Replacement is manager-only.
4. A finite reconciliation job claims indexed due attempts/refunds using leases and
   SKIP LOCKED. Read-only provider HTTP happens after claim commit. Webhooks and
   inquiry observations use the same financial processor with different provenance.
5. Extend `/v1/manager/financial` with bounded inventory/detail, idempotent payment
   and refund inquiry, and refund approval. Actors come from live MANAGER authorization.
   Persist command/audit identity, submitted reference, evidence and decision. No
   direct status-setting API or screenshot attestation.
6. Financial mutations lock Payment before Attempt/Charge/Obligation/Refund. Work-unit
   and request locks follow Payment; planning never acquires financial ancestors.
   External calls hold no transaction. Provider identity and command uniqueness are
   backstops; contradictory refund success blocks subsequent payouts.
7. Cadence, maximum backoff, batch size, lease and unresolved threshold require explicit
   configuration. No production cadence is inferred from the policy's proposals.
   No additional Azure resources or paid calls; bounded existing ACA Job entry point.
8. Emit identifier-only CustomerFinancialStatusChanged outbox events at financial
   commits. Existing rider push cannot claim customer delivery: customer session-linked
   registration, consumer routing, credentials and mobile refresh need separate work.
   This task adds durable events without an unbounded delivery subsystem or frontend edits.
9. Migration 0020 adds entities and nullable metadata without guessed capture/refund
   backfill. Legacy matching requires verified provider facts and manager audit.
   Downgrade removes new accounting; export financial evidence before rollback.
10. Customer DTO enums/paths remain unchanged. Expiry releases booking eligibility,
    retaining unresolved Payment and attempts; a late verified capture records financial
    success plus a manager exception without reviving the slot. Charge-aware reads
    replace the old global/canonical refund cap. Existing refund worker/outbox are reused.

Provider verification: GET order, GET order payments, GET payment and GET refund are
documented by [Orders inquiry](https://razorpay.com/docs/api/orders/fetch-payments/),
[Payment inquiry](https://razorpay.com/docs/api/payments/fetch-with-id/) and
[Refund inquiry](https://razorpay.com/docs/api/refunds/). Order creation is not capture.
[Refund entity](https://razorpay.com/docs/api/refunds/entity) distinguishes pending,
processed and failed but does not establish a universal non-payable guarantee for
failed operations. Replacement must fail closed until normal-refund finality is
explicitly confirmed for the configured account; lookup failure/timeout is never proof.

## Implementation and validation

## 1. Delivery identity and scope

Base commit: `e00921c1563884eb4989d2ab2faa9bbe43f36cc3`. Branch:
`phase2/customer-financial-cancellation`. Final implementation commit is the local
commit containing this report; its exact SHA is supplied in the delivery response
and can be obtained with `git rev-parse HEAD`. No push, merge or deployment.
Preflight verified the expected HEAD and clean tracked tree, supplied policy inputs,
Batch A/provider ancestors, migration 0019, Batch B report and readable frontend.
The exact final SHA and clean post-commit working tree are verified in the delivery response. As in Batch B, embedding a report commit's own SHA would change that SHA; the report identifies it by its containing commit.

## 2. Approved policies and prior authority

The three supplied documents are preserved at
`PAYMENT_RECONCILIATION_AND_MOBILE_STATUS_EVENTS.md`,
`FAILED_REFUND_RECOVERY_POLICY.md`, and `HISTORICAL_FINANCIAL_EXCEPTIONS_POLICY.md`.
Added repository references distinguish their approved business policy from delivered
mechanisms and external prerequisites. ADR-005, architecture, domain, schema, ER and
idempotency documents explicitly identify the superseding approved decisions.
Historical Batch B results and limitations remain history; no old result was rewritten.
The current task authorizes the small schema/API extensions proposed in the policies.
No COD, Pay at Pickup, second financial processor or new provider is introduced.

## 3. Final relational model and migration

Payment remains one logical quoted booking payment; PaymentAttempt remains one
checkout/order attempt. Existing Refund remains one provider operation with its
original stable UUID, receipt and native idempotency key. Migration
`0020_financial_reconciliation` follows `0019_customer_financial_events` and adds:

- CapturedCharge: unique provider/payment ID, Payment and Attempt FKs, verified
  amount/currency/order/account binding, first evidence FK and capture timestamp.
- RefundObligation: unique charge FK, positive owed amount, reason and payout block.
  Outstanding money is owed amount minus successful linked refunds; failed operations
  do not erase the obligation. Reserved money includes uncertain/unverified failures.
- FinancialException: unique business case key, typed payment/charge/refund/evidence
  FKs, reason, OPEN/RESOLVED and timestamps.
- FinancialAudit: unique manager command UUID, acting user/payment/refund/evidence
  FKs, submitted reference, action, decision and timestamp.
- Nullable charge/obligation/failure evidence on existing refunds; account/provenance/
  outcome on minimal events; indexed next-check and lease metadata on attempts/refunds.
- A cancellation authorization marker, false for historical rows. Only a fresh
  authorized cancellation sets it; old cancellation history never implies new consent.
- A PostgreSQL charge reservation trigger. It locks the actual charge, validates
  ancestors/provider/currency/obligation and bounds aggregate reserved money, including
  canonically mapped legacy operations. Outcome updates record external truth, even
  if a provider contradicts earlier finality; they do not hide a real payout.

Upgrade seeds scheduling only for unresolved existing attempts and non-successful
refunds. It manufactures no captured IDs, amounts, obligations, approvals or payouts.
Nullable links preserve old rows. Downgrade retains original Payment/Attempt/Refund/
Event history but drops the new accounting/evidence: export it before any real rollback.
No PostGIS table or existing index is removed.

## 4. API contracts and manager behavior

All paths require existing live MANAGER authorization; actors come from authenticated
principals, never a body-supplied user. Mutating bodies reject extra fields.

| Method/path | Contract |
|---|---|
| GET `/v1/manager/financial/payments` | UUID cursor, limit 1..100; pending payments, open cases and cancelled history lacking refunds; identifiers and financial facts only |
| GET `/v1/manager/financial/payments/{payment_id}` | Bounded attempts, charges, refunds, cases, audit and minimal evidence; truncated collections labelled; obligation balance uses database aggregation rather than truncated refund lists |
| POST `/v1/manager/financial/payment-attempts/{attempt_id}/reconcile` | command UUID plus optional syntactically valid reported Razorpay payment ID; verify ownership/current provider facts, persist inquiry audit |
| POST `/v1/manager/financial/refunds/{refund_id}/reconcile` | command UUID; uses original refund/charge references; cannot supply a different payment ID |
| POST `/v1/manager/financial/charges/{charge_id}/refund-approvals` | command UUID and optional failed refund UUID; audited historical recovery or replacement, never direct money-status CRUD |

Audit responses link the decision to provider evidence. Detail exposes its source,
observed outcome, processing decision and reason, plus durable exception reasons.
Exact completed command replay returns the audit without another GET; a changed
fingerprint conflicts. Authorization is checked before inquiry and again before payout
authorization. Provider unavailability is sanitized; no screenshot/raw provider body is
stored. Financial approval conflicts return 409 and persist refused approval history.

## 5. One authoritative processor and provider inquiries

HMAC webhook events, authenticated API inquiries and refund provider responses all
converge through `process_authenticated_payment_event`. Their provenance remains
WEBHOOK, API_INQUIRY or PROVIDER_RESPONSE; a GET is never labelled an authenticated
webhook. GET order validates stable local receipt, order ID, money and currency;
order-payments and optional GET payment establish actual capture and association.
Created/authorized payments stay unresolved. Mixed failed/unresolved instruments do
not allow a new online charge. Missing/ambiguous/malformed/rate-limited/network results
remain unresolved, with backoff and durable operational escalation. Order creation is
not payment capture. Canonical first success funds one booking, subject to eligibility.

Account binding uses a hash of configured merchant account ID, or the configured
public key ID if no account ID is supplied. Authenticated API access verifies ownership;
configured webhook account identity is also checked. Use a stable confirmed account ID
before enabling reconciliation/key rotation. References supplied by a manager must
match the persisted attempt/order and exact amount/currency; they are not proof alone.

## 6. Bounded automated reconciliation and scheduling

Due indexed attempts/refunds are claimed with SKIP LOCKED, UUID tokens and deadlines,
then committed before any provider GET. A finite batch is shared between refund and
payment work; one financial parent never serializes all customers. Expired leases are
recoverable without process memory. Claim release checks its token, advances bounded
exponential backoff with jitter, and stops successful/definitively failed payment work.
Long unresolved outcomes create one durable case/customer status event. Failed refund
operations remain inquiry-eligible; they are never automatically replaced.
Terminal verified outcomes close the associated uncertainty cases; settled refunds also
close their prior failure case while retaining failure evidence and refused approvals.
Commercial, identity, finality-contradiction and late-booking cases retain manager review.

`python -m tirodhan.workers.financial_reconciliation` is a finite manual/job entry point.
The existing `pending_payment_expiry` worker optionally runs the same sweep after its
booking expiry transaction closes. With all five financial scheduling values absent,
the phase does nothing; partial configuration fails closed. Existing command retention
and planning lead time are required too. No Terraform or Job resource changes.
The existing ACA cron schedule has minute resolution: shorter proposed intervals need
an explicitly reviewed schedule/entry-point change; they are not implemented by a
permanent sleeping process. Configure limits/cadence only after merchant rate-limit
confirmation. Enablement increases bounded GET/database work on the existing job, not
recurring infrastructure count. Batch selection avoids full historical scans.

## 7. Charges and additional compensation

Unique `(provider, provider_payment_id)` creates one record per real captured charge.
The quote, canonical attempt and original acceptance do not change for additional
success. Each additional verified, unambiguous charge gets its own full obligation,
one initial Refund and RefundRequested/customer event in the capture transaction.
The refund worker targets that actual captured payment ID, including a second capture
on the same checkout attempt. Duplicate evidence never creates another operation.
Multiple captures on cancellation are independently accountable: two 500 captures may
owe two 500 refunds. The obsolete combined logical-amount cap is not applied to them.
Partial/contradictory mapping retains the capture and opens review rather than losing
verified money. Existing failed operations do not cause an automatic replacement.

## 8. Refund failure and authorized replacement

Timeout, missing webhook and SUBMITTED/PROCESSING/INITIATION_UNCERTAIN reserve money.
Provider retries resume that same operation/receipt/key/body. A fresh read-only refund
inquiry can recover a missing reference through its stable receipt. Processed success
is final locally; delayed failure cannot make it replaceable.

Razorpay's public failed status alone does not prove universal non-payable finality.
`razorpay_normal_refund_failure_finality_confirmed` defaults false. Replacement requires
account-specific confirmation of that guarantee, a fresh API failed normal-refund
result, matching original charge/refund/money/account, persisted proof FK/timestamp,
an outstanding obligation and a complete externally queried refund inventory matching
local operations. Unknown legacy mapping or additional external refund refuses payout.
No network error, absent operation or old webhook is accepted as proof.

Live MANAGER approval creates a new Refund identity/native key, audit, idempotent
command completion and transactional outbox atomically. Payment/charge reservations
arbitrate competing commands: only one remaining balance can be reserved. Exact refusal
replay remains refused; changing circumstances needs a new command. Customer APIs and
automatic workers have no replacement authority. If an originally proven non-payable
operation later succeeds, record truth, block the obligation and raise a contradiction
case; refuse subsequent provider claims. Already-issued external operations cannot be
recalled by a database transaction, so provider finality is a real enablement prerequisite.
A renewed pending observation after verified failure also revokes its non-payable
reservation release and blocks an already-authorized replacement before provider POST.

## 9. Historical and late-payment recovery

Late expiry/cutoff/freeze capture is recorded as financial SUCCEEDED without accepting,
reopening or replanning the booking. A durable case and Operations outbox flag identify
the charge. The booking's expired slot stays released. A manager may freshly verify
capture and the complete empty refund inventory, then atomically approve a full refund
through the existing worker. This exceptional first late capture is not auto-refunded.

Legacy cancelled requests lacking refunds appear in the bounded manager inventory.
Inquiry discovers actual charge facts and creates a case; replay creates no payout.
Historical recovery requires fresh provider verification, safe accounting and audited
approval. Unmapped legacy refunds, external operations or insufficient evidence stay
under investigation; no guessed IDs/amounts or broad monetary backfill. Fresh customer
cancellation retains approved automatic compensation. Additional excess captures keep
the separately approved automatic full-compensation rule.

## 10. Locks, crashes and concurrency

All overlapping financial mutations lock Payment before Attempt/Charge/Obligation/
Refund and then work-unit advisory/request locks where needed. The shared processor
first resolves immutable parents; event insertion uses null descendant FKs until matching
is locked. Multiple candidate parents/descendants are locked in stable UUID order.
Planning takes work-unit/request locks and never subsequently requests Payment.
Cancellation/expiry cannot form a cycle through a financial ancestor. Scheduling claim
transactions lock only their own rows and commit without requesting Payment.

Manager authorization locks follow Payment; command reservations are short and commit
before GET. Native execution claims and uncertain outcome persistence also take Payment
first. Refunds are refreshed after locking, so an in-flight timeout cannot overwrite a
webhook success. Provider HTTP holds no database transaction/connection. Financial and
outbox mutations share commits; interrupted manager readonly inquiry can repeat safely,
and expired scheduler/native worker leases recover. No global mutex, Redis or new broker.

## 11. Expiry and existing customer contracts

The five-minute frontend waiting timer performs no backend financial transition. Booking
expiry changes CollectionRequest to EXPIRED and releases eligibility; unresolved Payment
remains PENDING, with its attempt scheduled for verification. It never becomes FAILED
solely because time elapsed. Existing CONFIRMING projects unresolved expired attempts
with online retry disabled. Definitive provider failure permits retry only when original
booking eligibility still holds. Late capture shows settled financial truth plus durable
Operations resolution; no automatic extension/rescheduling.

Existing customer payment/refund/detail endpoints and DTO enums remain intact. Charge-aware
batch reads use a fixed extra set query rather than SQL per collection, validate per-charge
reservations, and preserve coherent authoritative snapshots. No access/refresh token,
session, phone/OTP or ownership contract change. Unsupported partial/adjustment historical
policies remain fail-closed rather than relabelling customer facts.

## 12. Transactional status events and actual push boundary

Identifier-only CustomerFinancialStatusChanged events cover payment confirmed/failed,
operationally unresolved money, refund initiated/completed/failed, manager exception
resolution and cancellation/expiry. Deterministic keys and Payment locking yield one
logical event per transition; publication/transport remains at-least-once. Existing
CollectionRequestAccepted and RefundRequested continue unchanged. FinancialExceptionRaised
is a durable Operations flag, not a claim that an Operations notification was delivered.

No customer event is emitted before its authoritative commit. Payloads contain request ID
and change code (exception events contain case ID), no phone, GPS, address, bank screenshot,
token, card or provider credential. This task adds no continuous polling/WebSocket/SSE.
The current publisher routes/notification consumer do not deliver these customer events.
Pending events need reviewed customer consumer routing; session-linked customer device
registration/ownership, FCM/APNs configuration, customer delivery worker and mobile receipt
refresh are still dependencies. Existing rider push is not customer delivery. No FCM/APNs
or Azure provisioning, device acceptance or end-to-end push claim.

## 13. Validation and schema-tooling findings

- Ruff lint passed; Ruff format check passed for 285 files; strict mypy passed for
  145 source files; `git diff --check` passed. No dependency or IaC change.
- Final focused reconciliation PostgreSQL suite: 29 passed, 33.23 seconds, including
  concurrent webhook/pull/manager convergence, per-charge caps, replacement races,
  real session RBAC, crash recovery, expired leases, minimal events, renewed pending
  after failed finality, case settlement and populated migration rollback.
- Configuration and existing job unit checks: 9 passed, 6.65 seconds. Missing scheduling
  is inactive; partial/invalid/unbounded policy fails before provider side effects.
- Initial complete run: 942 passed, 4 failed, 1 skipped; 471.83 seconds. Three old
  freeze/cutoff assertions expected pending booking/money after capture; the fourth
  treated unverified failed refund as released money. Their expected values were
  updated to the explicitly approved policy, without weakening those business races.
  The revised policy/race and recovery selection passed 34 tests in 80.80 seconds.
- Final full regression on the final implementation: **959 passed, 1 skipped, zero
  failures**, 458.99 seconds. JUnit confirms **515 PostgreSQL integration tests passed**
  and **444 unit tests passed / 1 skipped**. The skip is the existing Windows/POSIX
  signal-exit test; four warnings are existing Firebase SDK deprecations. This includes
  auth/2Factor, customer APIs, booking/availability, Razorpay, cancellation, planning,
  compaction, dispatch, fulfilment, media and migrations. Two interim runs were stopped
  during final review to add case-settlement/finality protections; no partial or
  interrupted run is reported as the complete final regression.
- Migration 0020 downgrade to 0019 and upgrade to head succeeded. The populated
  migration integration test preserves nonempty original Payment, Attempt, Refund and
  Event row counts and original refund amount; new accounting is deliberately removed
  by downgrade, not silently reconstructed. Single current/head revision is
  `0020_financial_reconciliation`. The direct PostgreSQL reservation test rejects a
  second intent exceeding its captured charge.
- **Raw `alembic check` fails on exactly three proven baseline discrepancies:**
  extension-owned `spatial_ref_sys`, `ix_collection_request_pickup_location_gist`, and
  `ix_collection_request_planning_batch_id`. PostgreSQL catalog inspection confirms
  PostGIS ownership of the table and the GIST/B-tree definitions of both indexes.
  Migration 0007 defines the indexes; their corresponding ORM metadata omissions and
  migration environment predate this task. None was removed. A separate comparison
  excluding exactly those reflected objects passed with **zero remaining differences**.
  This diagnostic filter is not a new permanent ignore rule or changed Alembic setup.

## 14. External prerequisites, risks and review decisions

- Confirm normal-refund failure finality with Razorpay for this merchant before enabling
  replacement. Keep the feature false until evidence of that guarantee exists. Public
  refund entity/FAQ documentation distinguishes states but does not supply the guarantee.
- Confirm stable merchant account binding, capture mode, read API permissions, webhook
  subscriptions/secrets and account-specific rate limits. Configure batch/backoff/lease/
  unresolved threshold/retention/planning lead time, then validate nonprod real callbacks.
- Collections containing 100 or more returned provider records fail closed; this slice
  does not assume a partial inventory is complete or invent unbounded pagination.
- Legacy unmapped/partial refund history and arbitrary Operations adjustments need an
  explicitly approved matching/commercial resolution. Refusal is safe, not a fabricated
  recovery success. Additional captures after terminal success still require an incoming
  webhook or explicit manager inquiry; the due sweep targets unresolved operations.
- Provider truth may contradict a finality guarantee. Preserve evidence and block further
  payouts; already-in-flight provider effects cannot be reversed locally. Monitor these
  cases operationally before real money enablement.
- Customer push and Operations notification consumption remain pending separate approval
  and implementation. The backend outbox/inventory are ready for review, not device-tested.
- Raw Alembic baseline discrepancies remain explicit; never drop spatial_ref_sys/indexes
  to make tooling green. New migration/accounting evidence needs an export before rollback.

## 15. Frontend and infrastructure confirmation

This task performs no writes in `C:\Users\91956\Tirodhan-frontend\tirodhan-frontend`;
its independent work is preserved. No Terraform, GitHub deployment workflow, Azure
resource, queue, database platform, OTP provider, paid external service or dependency
change. Existing modular FastAPI monolith, PostGIS, Service Bus Standard and ACA Jobs/
workers remain. No paid provider call was made: external tests use HTTP mocks.
The final read-only frontend check is clean at
`6a66b7f3749ac664bd30680f3090c1be81890ec1`; no authorship is attributed to independent
frontend history. Backend diff against the base contains no infrastructure/deployment,
dependency, migration-environment, migration-0007 or collection-model changes.
Stop after local commit for architectural review; do not push, merge or deploy.

## 16. Exact changed files

- `docs/ARCHITECTURE.md`
- `docs/DOMAIN_MODEL.md`
- `docs/ER_DIAGRAM.md`
- `docs/FAILED_REFUND_RECOVERY_POLICY.md`
- `docs/HISTORICAL_FINANCIAL_EXCEPTIONS_POLICY.md`
- `docs/IDEMPOTENCY.md`
- `docs/PAYMENT_RECONCILIATION_AND_MOBILE_STATUS_EVENTS.md`
- `docs/PHASE_2_FINANCIAL_RECONCILIATION_COMPLETION_REPORT.md`
- `docs/SCHEMA_DESIGN.md`
- `docs/adr/ADR-005-payments-refunds.md`
- `migrations/versions/0020_financial_reconciliation_financial_reconciliation.py`
- `src/tirodhan/api/router.py`
- `src/tirodhan/api/routes/manager_finance.py`
- `src/tirodhan/core/config.py`
- `src/tirodhan/modules/collection_requests/cancellation.py`
- `src/tirodhan/modules/collection_requests/expiry.py`
- `src/tirodhan/modules/customer_reads/finance.py`
- `src/tirodhan/modules/customer_reads/projections.py`
- `src/tirodhan/modules/payments/accounting.py`
- `src/tirodhan/modules/payments/models.py`
- `src/tirodhan/modules/payments/ports.py`
- `src/tirodhan/modules/payments/razorpay.py`
- `src/tirodhan/modules/payments/reconciliation.py`
- `src/tirodhan/modules/payments/refunds.py`
- `src/tirodhan/modules/payments/service.py`
- `src/tirodhan/workers/financial_reconciliation.py`
- `src/tirodhan/workers/pending_payment_expiry.py`
- `tests/integration/conftest.py`
- `tests/integration/razorpay_helpers.py`
- `tests/integration/test_cancellation_refunds.py`
- `tests/integration/test_collection_payment.py`
- `tests/integration/test_customer_financial_cancellation.py`
- `tests/integration/test_customer_mobile_core.py`
- `tests/integration/test_financial_reconciliation.py`
- `tests/integration/test_planning_foundation.py`
- `tests/integration/test_razorpay_payments.py`
- `tests/unit/test_financial_reconciliation_policy.py`
