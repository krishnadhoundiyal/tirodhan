# Tirodhan — Phase 2 financial merge record and operations runbook

**Status:** Merged-code documentation; **NOT authorization to enable live-money features or deploy.**
**Baseline reviewed:** `3aa45bc75b92b71609dcbcfff5fbe6a2410e7f48`.
**Scope:** customer cancellation compensation, payment/refund reconciliation, Razorpay normal refunds, financial exceptions, manager recovery, webhook/queue ingestion, and settlement inventory.
**Authority:** [Failed Refund Recovery Policy](FAILED_REFUND_RECOVERY_POLICY.md), [Historical Financial Exceptions Policy](HISTORICAL_FINANCIAL_EXCEPTIONS_POLICY.md), [ADR-005](adr/ADR-005-payments-refunds.md), [Idempotency](IDEMPOTENCY.md), [Financial Review Final Closure](PHASE_2_FINANCIAL_REVIEW_FINAL_CLOSURE.md). This runbook documents implemented behavior and release gates; it does not silently change commercial policy.

## 1. Financial principles and identities

- A verified `CapturedCharge` represents each distinct provider capture; one logical booking acceptance never erases an additional real capture.
- `RefundObligation` represents customer money still owed; `Refund` represents an individual provider refund operation. A manager's approval is **authorization**, not verified repayment.
- An original refund is identified by its immutable Tirodhan Refund UUID, Razorpay payment ID, minor-unit amount, currency, receipt `rf_<refund_uuid.hex>`, note `tirodhan_refund_id`, and stable native `X-Refund-Idempotency` key (with the documented legacy-wire mapping). Preserve the original request body for retries.
- Different Refund UUIDs are **replacement economic operations**; they require independently verified definitive non-payability of the old operation, manager authorization, sufficient reservations, and existing row-lock/database safeguards. An uncertain original is never replaced merely because a request timed out or GET returned no matching refund.
- The transactional outbox, durable inbox, charge uniqueness, Payment-first PostgreSQL locks, audited manager commands and provider evidence serve distinct purposes; Service Bus at-least-once transport is not distributed exactly-once execution.

## 2. Original-refund recovery decision

| Condition | Allowed action | Financial interpretation |
|---|---|---|
| New, genuinely unclaimed `PENDING` original | Preflight read-only provider GET/charge-refund inventory; then first POST if still authorized and safe | Intent is not repayment |
| Original accepted, response lost; authentic matching webhook or complete GET identifies it | Persist provider refund ID/outcome using the shared financial processor; **no additional POST** | Correct original operation recovered |
| Claimed original; complete GET finds no unique match; replay window configured and still valid | Recheck provider refund inventory, local holds, status, account, existing operations, and replay deadline; then POST the **same** original request/key | Conditional same-operation replay, **not** replacement |
| GET times out, is incomplete, conflicting, ambiguous, or cannot prove a matching identity | Fail closed; keep original uncertain, retain liability, continue reconciliation and Operations attention | No conclusion about remote acceptance |
| Claimed original; window unset or no longer sufficient to cover request budget | Do not POST; retain `INITIATION_UNCERTAIN`, raise/retain `REFUND_REPLAY_RETENTION_UNCONFIRMED`, expose Operations case | Execution currently blocked, not a proven provider failure |
| Any later authenticated `refund.processed` or conclusive original-operation GET | Update actual verified outcome on the original Refund even if previously blocked for replay | Read-only reconciliation continues |
| Actual definitive, provider-verified non-payable failure | Keep obligation outstanding; manager may authorize a **new** operation only under approved replacement policy | Failure is not customer repayment |

### 2.1 Configurable replay window — owner-approved clarification

Configuration field: `razorpay_refund_replay_window_seconds`; environment variable: `TIRODHAN_RAZORPAY_REFUND_REPLAY_WINDOW_SECONDS`. It is a **positive optional integer in seconds**, **not** a hard-coded five-day rule. The default is unset, so uncertain-operation POST replay is disabled.

Eligibility is **recomputed using the currently configured safe window** against the immutable first `processing_started_at` timestamp. The worker also budgets the configured HTTP timeout before dispatch; the Razorpay adapter checks the deadline at the real HTTP boundary. A successful fresh GET always takes precedence over replay. A local Refund in `SUCCEEDED`, `SUBMITTED`, or `FAILED` is not made replayable simply by enlarging the window.

**Example (illustrative, not an approved live setting):** an original claimed six days ago is ineligible with a five-day configuration. If later set to eight days, the same unresolved original may become eligible **again** after a fresh complete GET, current holds/merchant-account checks, time-budget checks and verified provider protection. Its timestamp, UUID, key, receipt and payload do not reset. Conversely, a reduced window can make a previously eligible uncertain original ineligible immediately.

**No irreversible `UNRESOLVED_EXPIRED` database status is required or approved.** The existing `INITIATION_UNCERTAIN` status plus a retained replay-retention exception represents the blocked condition. An Operations investigation can re-queue the original using `recover-original` only if the current safety rules are met; it cannot override financial holds or create a new operation. The exception reason must not be interpreted as an immutable expiry decision.

**Provider-contract gate:** Razorpay's public Payment Gateway normal-refund documentation supports identical-key/body idempotent retries but does **not establish a guaranteed minimum key-retention period**. A published seven-day RazorpayX *payout* retention rule is not a normal-refund guarantee. Do not enable or enlarge the replay window in live environments until Razorpay gives refund-specific written evidence and an adequate margin is justified. Changing local configuration does **not** extend provider retention or prove non-acceptance from an empty GET.

**Current regression evidence:** `test_claim_never_posted_can_recover_only_original_identity` proves default blocking and recovery under a synthetic configured window; `test_native_deadline_is_rechecked_immediately_before_http` proves the HTTP-boundary deadline check. These do **not** establish Razorpay's live retention contract. A distinct test that first expires an original and later increases configuration is not reported in the reviewed closure suite; this scenario is an explicit follow-up regression candidate, not a claimed passed test.

### 2.2 Lost response versus never-received POST

These are indistinguishable from the local claim alone. The worker writes `processing_started_at` **before** provider I/O, so a crash immediately after commit may leave `INITIATION_UNCERTAIN` without any remote refund. The other possibility is accepted provider refund with lost response and delayed visibility. GET absence is inconclusive in both cases. Do not rotate the key, create a fresh refund ID, mark the old one FAILED or discharge the obligation based only on this absence.

## 3. Webhook, consumer and independent discovery

1. FastAPI authenticates exact raw Razorpay HMAC bytes and account/event identity. The webhook HTTP route sends an authenticated minimal envelope to the dedicated Service Bus queue and returns success only after durable broker acceptance.
2. The consumer commits durable inbox, provider event evidence, captures/refunds and outbox atomically in PostgreSQL, then acknowledges the broker; duplicate and out-of-order messages cannot themselves create duplicate charge/refund business effects.
3. An actual processed provider refund is correlated only by reliable native Refund ID or validated original correlators plus matching financial/merchant identity. Payment ID + amount alone is insufficient; conflicting claims stay evidence and block unsafe payouts.
4. Reconciliation GETs unresolved orders/refunds independently of delivery. Account-wide payment and refund inventory and revisited historical creation windows discover covered missed captures/external refunds. Settlement reconciliation retains typed evidence and relevant mismatches.
5. Evidence gaps remain: live webhook cold-start deadline, visibility lag, subscription/rotation details, exact merchant report completeness, API rates and account-specific retention/finality. Enabling jobs is a deployment/Operations responsibility, not established by local mocked tests.

## 4. Manager and Operations runbook

All actions below require an active `MANAGER` principal and an idempotent fresh `command_id`; provider reads occur outside database transactions, followed by authoritative rechecks under Payment-first locks.

| Investigation | Entry point under `/v1/manager/financial` | Required evidence/action |
|---|---|---|
| Outstanding or held customer refund | `GET /payments`, `GET /exceptions`, `GET /payments/{payment_id}` with bounded child cursors | Confirm charge, obligation, reserved/paid balance, current status, related case and provider evidence |
| Known original outcome uncertain | `POST /refunds/{refund_id}/reconcile` | Fresh read-only GET; never a new payout identity |
| Never-sent, lost-response or DLQ original | `POST /refunds/{refund_id}/recover-original` | Fresh GET; re-queue same original only if currently safe; otherwise audited `PROVIDER_BLOCKED` |
| Verified non-payable original | `POST /charges/{charge_id}/refund-approvals` | Fresh definitive non-payability, complete relevant inventory, live permission, remaining balance, immutable audit |
| Mechanically correctable financial hold | `POST /exceptions/{case_id}/resolve` | Fresh matching provider/refund/report facts, current case occurrence, complete submitted-operation inventory, no other unresolved holds |
| Unknown provider event | `GET /provider-events`; `POST /provider-events/{evidence_id}/reconcile` | Fresh account-bound provider GET; never invent a Payment or Refund from incomplete metadata |
| Unknown settlement/adjustment | `GET /settlement-evidence`; `POST /settlement-evidence/{evidence_id}/reconcile` with report day | Validate bounded report membership/classification; retained `OWNER_DECISION_REQUIRED` where policy is absent |
| Backlog or execution failure | `GET /refund-deliveries`, `GET /scan-checkpoints`, financial exception queue, broker/DLQ inspection | Repair failed job/credentials/message path, confirm current provider evidence and restart bounded workers |
| Aging and escalation | Exception queue and `tirodhan.financial_operations` JSON attention records | Assign human responder/acknowledgement and monitor repeat aged cases; emitted logs do not prove somebody responded |

**Manager inactivity does not forgive debt.** Old cases remain queryable, may receive repeated configured attention, and cannot be marked financially settled merely through authorization. An authenticated late processed webhook or GET may close the technical uncertainty after earlier replay blocking; disputed or external-credit commercial cases require separate policy.

## 5. Validation recorded for the reviewed implementation

- Codex reported **1,040 passed, 1 pre-existing Windows/POSIX skip, 4 existing warnings**, including **35 financial-review closure integration cases**, on disposable PostgreSQL; this authoring/merge step did **not** rerun the suite.
- Ruff, formatting, mypy and Terraform validation were reported passing. No Terraform apply or real Razorpay money operation occurred.
- Migration `0022_financial_review_recovery` adds exception recurrence/attention timestamps, evidence-targeted financial audit relationships and scan checkpoint diagnostic fields. It does not add a blanket record version or a new paid data service. Populated downgrade correctly refuses to discard audit provenance.
- Codex's raw Alembic comparison still identified **three pre-existing schema differences** (`spatial_ref_sys` and two migration-0007 indexes), with **zero additional differences** after excluding exactly those known items.
- The original closure ledger comprises **54 findings: 19 fixed/tested, 2 proven safe, 23 provider-blocked, 7 operational-enablements pending and 3 owner decisions**. Only the first two categories denote local closure; none of this establishes live-money readiness.

## 6. Release gates and ownership

| Gate | State at merge | Owner/next step |
|---|---|---|
| Razorpay normal-refund idempotency minimum guaranteed retention / original-key scope (Q4/Q5) | **Unverified; replay remains unset** | Owner and Razorpay technical/support contact obtain written, merchant-specific contract; configure strictly shorter window and bounded HTTP budget after verification |
| Definitive failure/non-payability for enabled payment rails (Q1/Q2) | **Unverified; replacement-finality flag remains false** | Razorpay confirms exact semantics before enabling replacement |
| Refund receipt/notes completeness, Dashboard coverage, merchant identity and visibility/rates (Q7–Q13) | **Unverified for this merchant** | Obtain samples, account-specific proof and nonprod tests; control Dashboard payout permissions |
| Settlement report availability/completeness, tax, historical corrections (Q14) | **Not fully established** | Validate merchant reports and approval policy before net/missing-settlement conclusions |
| Commercial disposition for external credits, partial refunds, disputes, adjustments, alternative repayment/rescheduling | **Owner decisions required** | Approve explicit policies; retain existing holds meanwhile |
| ACA/API cold starts, webhook ack, queue identity, worker schedules, DLQ, log shipping, responder/SLA | **Operational enablement pending** | Deploy and observe in nonprod, appoint Operations owner; no silent paid infrastructure |
| Dynamic configuration-expansion regression | **Specific sequence not separately documented as executed** | Add a focused PostgreSQL test at a future authorized code change; do not infer an externally guaranteed outcome |

## 7. Merge versus deployment

Merging this branch into `main` records reviewed implementation and documentation. It **does not** switch on unverified provider flags, configure a positive refund replay duration, provision Azure resources, clear unpaid debt, or certify live-money readiness. Complete the release gates and independently verify the active environment before conducting real-money operations.

## 8. Source of truth

- Detailed findings, evidence matrix and validation commands: [Phase 2 Financial Review Final Closure](PHASE_2_FINANCIAL_REVIEW_FINAL_CLOSURE.md).
- Failure/replacement policy: [Failed Refund Recovery Policy](FAILED_REFUND_RECOVERY_POLICY.md).
- Late-capture and operational accountability policy: [Historical Financial Exceptions Policy](HISTORICAL_FINANCIAL_EXCEPTIONS_POLICY.md).
- Transaction/transport/replay constraints: [Idempotency](IDEMPOTENCY.md).
- Refund runtime: `src/tirodhan/modules/payments/refunds.py`, provider: `razorpay.py`, manager recovery: `manager_recovery.py`, web routes: `src/tirodhan/api/routes/manager_finance.py`.
- Focused tests: `tests/integration/test_financial_review_closure.py`, `test_razorpay_refunds.py`, `test_financial_correctness_closure.py`.


## P1-1 preparation and historical Order recovery

The authorized P1-1 branch requires independent review before PR/merge. See [API/Operations
contract](PAYMENT_PREPARATION_API.md) and [implementation evidence](PAYMENT_P1_1_IMPLEMENTATION_REPORT.md).
No rollout or live financial operation occurs in the implementation task.

Migration 0023 adds short Checkout permission, observation fencing, first-POST permission and
initiated-term guards. It rewrites no financial outcomes. Configure PAYMENT_CHECKOUT_READINESS_SECONDS
explicitly (1..60, no default) and planning lead. Neither value establishes financial finality.
Verify merchant account identity before key rotation. Deploy a compatible API/worker runtime;
do not serve old Checkout logic with the new preparation contract.

Bounded recurring reconciliation includes FAILED established Orders with null next_check_at.
Null due times use creation time for fairness. Fresh provider inquiry establishes current evidence;
empty/unavailable inquiry changes no financial truth. Missing/mismatched merchant bindings are
rescheduled and receive configured aged-case attention without cross-account GET. Operations
must verify ownership through existing audited recovery. Never bulk FAILED->PENDING, rotate
receipts, delete cancelled attempts or disable financial discovery after cancellation.

Explicit cancellation of A permits independently eligible B to pay while A remains unresolved.
Eligible actual A charges receive automatic full per-charge compensation; held/contradictory
cases stay in Operations. Refund submission is not bank payout. A's slot is not reserved for B.
Mobile retains one stable new clientRequestId; different IDs are distinct booking commands.

Normal-refund replay retention and definitive non-payability gates remain unset/false by default.
No new paid infrastructure is introduced. Monitor bounded provider GET traffic, rates/cadence,
account rotation and response visibility in nonprod. Independently scheduled P1-2 already-refunded
money acceptance and P2-6 manager throttling remain outside this closure.
