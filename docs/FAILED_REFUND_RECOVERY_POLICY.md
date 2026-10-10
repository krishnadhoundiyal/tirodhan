# Tirodhan — Failed Refund Recovery Policy

**Status:** Approved business policy; implementation design and integration pending

**Scope:** Refund obligations, provider reconciliation, and manager-authorized replacement refund operations

**Suggested backend repository path:** `docs/FAILED_REFUND_RECOVERY_POLICY.md`

**Related:** `docs/adr/ADR-005-payments-refunds.md`, `docs/PHASE_2_BATCH_B_REPORT.md`, payment reconciliation and mobile status events specification

## 1. Executive decision

A refund retry (a **new provider refund operation**) is permitted **only after conclusive confirmation that the prior refund operation definitively failed**. Uncertain, processing, or submitted refunds must instead be reconciled against Razorpay using their existing identity. For V1, a verified definitive failure requires **manager authorization** before a replacement refund operation is created. The customer cannot initiate retries. No manager may simply edit the refund status.

Distinguish two concepts:

- **Refund obligation:** the enduring amount owed to the customer, arising from a valid cancellation or another authorized financial correction.
- **Refund operation:** a single submission to Razorpay to discharge an obligation. There may be a replacement after a definitively failed operation, but never two simultaneously payable operations for the same outstanding amount.

Existing Batch B `Refund` records represent provider operations and do not yet fully separate the enduring obligation from attempts. An explicit model/invariant review is required before implementation. This document specifies business behavior, not a claim that the current code implements it.

## 2. Quick-reference decision table

| Observed provider/backend state | Can create a new refund operation? | Automated action | Manager authority | Customer-visible interpretation |
|---|---|---|---|---|
| `PENDING` (not sent) | **No** | Existing worker processes durable outbox request | Inspect only | Refund initiated |
| `PROCESSING` | **No** | Reconcile same operation | Trigger verification | Refund processing |
| `SUBMITTED` | **No** | Reconcile same operation | Trigger verification | Refund processing |
| `INITIATION_UNCERTAIN` / lost response | **No** | Retry *read-only inquiry* or safe native recovery using the **same identity**, never create replacement | Investigate/trigger inquiry | Refund confirmation pending |
| `SUCCEEDED`, provider verified | **No** | Close obligation for verified amount | None | Refund completed |
| `FAILED`, but failure not established as final | **No** | Confirm exact provider refund outcome | Investigate | Refund under review |
| **Definitively failed** and verified non-payable | **Yes, following manager approval only** | Record failure and retain open obligation; await approval | Authorize controlled replacement | Refund failed; resolution in progress |
| Conflicting evidence or provider unavailable | **No** | Preserve open obligation and exception | Escalate; do not override | Refund under review |

**Clarification:** A worker's internal retry of the **same idempotent provider operation** is not the same as creating a new refund operation. Neither may be allowed to duplicate an external payout.

## 3. End-to-end recovery flow

1. Cancellation/other approved financial correction creates an obligation and a durable initial refund request **atomically** with its business transition and transactional outbox.
2. Refund worker executes the existing provider operation outside any database transaction, using its stable provider idempotency identity.
3. Provider webhook and scheduled read-only status inquiry converge on the same financial reconciliation service.
4. On success, record authenticated confirmation and discharge the obligation for the actual amount.
5. On timeout, missing response, or ambiguous provider result, **retain the original operation and its identity**; prohibit replacement. Continue reconciliation and flag if aging exceeds operational thresholds.
6. Only when a definitive failed, non-payable operation is verified does the case become eligible for manager review.
7. Manager sees amount, currency, canonical charge, original refund reference, verified provider outcome, history, and outstanding liability. Manager authorizes replacement with a reason and audit evidence.
8. Backend re-verifies eligibility transactionally; creates a **new** provider refund operation with its own stable idempotency identity and new outbox event. The old failed operation is immutable history. The outstanding obligation itself is **not duplicated**.
9. Worker handles the replacement asynchronously. Webhooks and inquiries reconcile it as usual.
10. If evidence later contradicts a supposedly definitive failure, stop further processing, flag an exception, and ensure no duplicate payout or silent liability closure.

## 4. Manager UI and permissions

| Screen/action | Required behavior |
|---|---|
| Financial Exceptions queue | Display open refund obligations, age, amount, state, source collection, and reason for review |
| Refund detail | Timeline of original and replacement operations, provider identifiers, verified outcomes and evidence |
| **Reconcile now** | Trigger read-only Razorpay inquiry without a new refund operation |
| **Authorize replacement** | Available only if definitive failure and unpaid balance are established; requires reason/evidence and authorization |
| Resolution history | Immutable actor, UTC time, action, verified provider facts, correlation IDs and results |
| Generic “mark paid/refunded” | **Not permitted** |

Customer messages and UI must distinguish **collection cancelled**, **refund in progress**, **refund confirmation pending**, **refund failed/under review**, and **refund completed**. A cancelled collection is not proof that the refund was paid.

## 5. Financial and concurrency invariants

- A verified captured charge and an approved refund policy are prerequisites for any refund obligation.
- Persist amounts in integer minor units and compare currency and merchant/provider ownership.
- `obligation_amount = verified_payouts + outstanding_amount` under the approved policy; never treat a definitively failed attempt as payout.
- No two concurrently payable operations may cover the same outstanding amount.
- Manager approvals are idempotent and bind to a specific obligation, current failure evidence, amount and charge.
- Financial row locks and database constraints protect balance, replacement uniqueness, and webhook/worker/manager races.
- All provider calls happen **outside** DB transactions; authorized intent plus outbox commits together.
- Webhook identity deduplication must not replace per-provider-operation idempotency.
- If the provider's finality guarantees are insufficient, do **not** authorize a replacement; escalate.
- Logs/audit store necessary identifiers and non-sensitive evidence references, not credentials, raw payment secrets or unredacted customer banking details.

## 6. Reconciliation triggers

Use the shared reconciliation architecture: provider webhooks, scheduled read-only Razorpay inquiries for unresolved operations, and on-demand manager inquiries. Exact cadence, provider limits, final-failure criteria, and reconciliation horizon are **configuration/verification tasks**, not fixed vendor guarantees. Notification events may prompt the app to refetch its authoritative refund read endpoint; push delivery is not financial authority.

## 7. Acceptance scenarios

| Scenario | Expected result |
|---|---|
| Initial refund succeeds | Obligation discharged once; no replacement |
| HTTP response lost but Razorpay accepted refund | Inquiry recovers same provider identity; no new refund |
| Refund in `PROCESSING` for hours | Reconcile; replacement blocked |
| Definitively failed refund, manager approves | Exactly one new operation and outbox event; old operation remains history |
| Manager double-clicks approval / retries request | One replacement operation, idempotent result |
| Two managers approve simultaneously | Converge to one replacement or an explicit conflict |
| Refund webhook arrives during manager approval | Locked revalidation prevents duplicate payable effects |
| Replacement fails again | Liability remains; fresh manager review required for any further replacement |
| Original unexpectedly succeeds after “definitive failure” | Flag reconciliation conflict, prevent/stop duplicate payout where still possible, require audited resolution |
| App misses push event | Next foreground or screen load fetches authoritative status |

## 8. Implementation delta and non-goals

**Needed:** distinguish durable refund obligation from provider attempts (whether through a separate minimal entity or well-justified equivalent); operation-level proof and idempotency; manager authorization endpoint and audit; provider status read interface; exception queue and tests. Reuse FastAPI, PostgreSQL, current outbox, Razorpay adapter and refund worker; no new service/queue/Redis.

**Not authorized:** new customer refund/retry command, arbitrary status override, automatic replacement on ambiguous outcome, unlimited retry loop, modifying frontend preview mode, or enabling unverified financial capabilities.

## 9. Outstanding implementation confirmations

Confirm Razorpay's exact terminal failure and retrieval semantics for the merchant integration; approved authorization roles/audit retention; refund obligation schema and migration; customer-facing reason mapping (including `OPERATIONS_ADJUSTMENT`); and end-to-end nonprod tests. These confirmations do not weaken the locked business rule above.


## Repository implementation references

The approved policy above is preserved. Implementation mechanisms and validation are
recorded in [the completion report](PHASE_2_FINANCIAL_RECONCILIATION_COMPLETION_REPORT.md).
See [ADR-005](adr/ADR-005-payments-refunds.md), [domain model](DOMAIN_MODEL.md),
[schema](SCHEMA_DESIGN.md), and [idempotency](IDEMPOTENCY.md). Proposed polling
intervals are not production defaults; normal-refund failure finality requires
provider/account confirmation before replacement can be authorized.
