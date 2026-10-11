# Payment P1-1 implementation and validation

Owner scope: IMPLEMENT, TEST, DOCUMENT, COMMIT; independent adversarial review comes next.
Branch: `codex/payment-p1-1-correctness`. Verified latest merged main/base:
`1cd137d62f6b3947c9d43d5ada5a4519ea6297b8` (PR #49). Initial checkout was clean.
The final delivery supplies the SHA of the commit containing this report.
No PR, merge, deployment, frontend change, real payment or additional paid infrastructure.

## Result and boundaries

Failed Razorpay instruments retain the original Order-level PaymentAttempt and scheduling.
Items, quote and money freeze at the first durable initiation intent. Same/fresh command keys
resume that identity or WAIT. Explicit cancellation then fresh booking B supports payment while
cancelled A remains unresolved. Verified late A captures stay on A and receive existing full
eligible charge-specific compensation; A remains CANCELLED and B is financially independent.
No QuoteRevision, cross-booking credit, partial settlement or refund algorithm replacement.

Direct dependencies P1-3 (terminal initiation replay), P1-4 (mutating planning guards) and P2-5
(merchant binding) are covered for the preparation/handoff paths reproduced here. This does not
claim independent closure of every earlier review scenario. P1-2 (already-refunded money funding
acceptance) and P2-6 (manager inquiry throttling) remain separate owner-supervised findings.
Neither was implemented or claimed closed.

## A1–A18 alignment

C/T references below identify exact source entry points and executable tests. All final validation
results appear in the validation section. Already Safe denotes retained behavior with executable
evidence. Qualifications distinguish local correctness from live merchant/mobile guarantees.

| Item | Result | Code and behavior | Tests / final validation | Residual qualification |
|---|---|---|---|---|
| A1 | Implemented | C1–C4, C18, C20: one exposed Order attempt, separate instrument events, DB-frozen terms | T1, T2, T15; **PASS**, source inspected | Snapshots do not prove future financial non-payability |
| A2 | Implemented | C2, C5: failed Razorpay instruments retain PENDING/due inquiry, no Order-level PAYMENT_FAILED | T1, T2, T9; **PASS**, source inspected | Late authorization remains possible |
| A3 | Implemented | C1, C12: Payment lock, shared bounded lease, stable recorded identity for exact/fresh keys | T1, T2, T20, T21; **PASS**, source inspected | Concurrent caller may receive WAIT before Order ID exists |
| A4 | Implemented | C3, C1, C9, C19: complete counts/money/state, expiring permission, observation fence, final READ COMMITTED joined check | T1, T3, T8, T16–T18; **PASS**, source inspected | External state may change after GET; handoff is not payment success |
| A5 | Implemented | C1, C4, C20: durable first-POST marker, ambiguous recovery by original receipt only | T14, T21; **PASS**, source inspected | Empty GET never proves absence; ambiguous legacy unsent intent needs Operations |
| A6 | Implemented | C7, C8, C1: eligibility before claim/POST and after I/O; in-flight real result retained | T4–T6; **PASS**, source inspected | Local cancellation cannot revoke an already-started remote call |
| A7 | Already Safe; recovery extended | C17, C2, C5: cancellation retains financial references, inquiry/inventory and compensation | T6, T9, T31; **PASS**, source inspected | Existing recurring jobs and inventory must remain operational |
| A8 | Implemented | C5, C6: bounded known FAILED/null-due selection, fair sorting/backoff, fresh evidence only | T9, T12, T38; **PASS**, source inspected | Unbound account skips HTTP and raises aged attention; no invented account/status |
| A9 | Already Safe | C17, C7: Payment-first cancellation/work-unit arbitration preserves uncertain exposure | T22, T23, T26; **PASS**, source inspected | Authoritative cutoff/freeze refusal remains |
| A10 | Already Safe | C2, C13–C15: per-charge capture, full eligible obligation, execution and outbox atomic | T10, T25, T27, T28, T41; **PASS**, source inspected | Holds, prior payouts and reservations still block unsafe duplicate compensation |
| A11 | Already Safe; explicit proof added | C16: fresh stable clientRequestId/context, server quote and current slot, exact replay | T10, T11; **PASS**, source inspected | A's original slot is not guaranteed for B |
| A12 | Already Safe; explicit proof added | C16, C1, C2: independent B can pay while cancelled A is unresolved | T10 (A first/B first/A unresolved); **PASS**, source inspected | Temporary double debit approved; no money transfer/credit |
| A13 | Implemented | C10–C12, C9: START/RESUME/WAIT/UNAVAILABLE, reasons, retry_allowed, verification_pending/deadline | T1, T7, T13, T17, T36; **PASS**, source inspected | Separate frontend must consume documented contract/fresh handoff |
| A14 | Already Safe; contract specified | C11, API/mobile docs: timer/readiness expiry does not set financial finality | T17, T37; **PASS**, source inspected | Bounded foreground refresh is a separate mobile implementation |
| A15 | Implemented | C7–C9, C1, C6: fresh planning/quote/ownership checks and exact merchant binding | T4–T8, T16; **PASS**, source inspected | Same verified account permits key rotation; legacy ownership never guessed |
| A16 | Implemented | C18, C20: three permission/fencing/crash fields, term guards; no new domain/infra | T15, T19; **PASS**, source inspected | Migration precedes new code; downgrade removes permission, not money history |
| A17 | Already Safe; executable proof retained/expanded | C2, C13–C17: both race winners, multiple charges, duplicate replay, holds and atomic outbox | T22, T25–T30, T34, T35, T41; **PASS**, source inspected | Held/uncertain refunds remain liability and Operations work |
| A18 | Already Safe; operational contract updated | C15, config/runbook/ADR: refund replay unset/finality false, audited recovery | T17, T32–T35, T39, T40; **PASS**, source inspected | Merchant-specific guarantees and live enablement require separate approval/evidence |

## Actual state machine and effect boundary

An established Order's instrument FAILED is evidence, not terminal failure: keep the attempt
PENDING and scheduled. Complete matching provider facts permit START for a created zero-instrument
Order, or RESUME if every instrument failed. Authorized/created instruments, unknown/incomplete/
contradictory inquiry and active preparation yield WAIT. Captures enter the existing atomic
financial processor; cancelled requests are not accepted. Only narrowly classified genuine
initial rejection with no Order releases identity for a fresh command. Exact historical rejected
replay stays that resource; after a newer exposure it is UNAVAILABLE/SUPERSEDED_ATTEMPT.
Legacy FAILED without this rejection proof and multiple exposed attempts cannot allocate an Order.

New intent commits order_creation_pending=true. After original receipt GET and fresh local
eligibility/account/lease checks, first-POST authorization commits it false before HTTP. Crash
before authorization can resume an unsent first operation; crash after authorization/lost response
can only discover original receipt. No absent lookup or age authorizes another POST. Shared DB
leases arbitrate preparation and reconciliation; no DB session stays open during provider I/O.

Verified permission lasts explicitly configured 1..60 seconds from snapshot time, clipped to
booking expiry. Newly mapped observations revoke permission/increment the counter. Final prep
checks version/evidence plus current booking/account/planning. Checkout uses READ COMMITTED,
READ ONLY and a final joined predicate query; a capture/auth/cancellation committed during earlier
reads revokes handoff. Other read DTOs retain repeatable-read coherence. Neither GET nor DB isolation
guarantees external immutability after response; old client Checkout cannot be remotely revoked.
Such late money remains on original A for reconciliation/compensation.

## API contract

- Existing POST `/v1/payments/collection-requests/{request_id}/attempts`, HTTP 201 and Idempotency-Key remain. Adds typed action, action_reason and nullable checkout_ready_until. Exact/fresh keys converge on exposed identity; WAIT is a preparation outcome, not financial failure.
- Payment read/detail adds these fields and verification_pending. retry_allowed is true only for START/RESUME; an established failed/unchecked Order projects CONFIRMING. CANCELLED collection and SUCCEEDED financial receipt are independent facts.
- Checkout GET adds checkout_ready_until alongside booking expires_at and existing public protocol fields. No HTTP/mutation occurs. Requires exact account, fresh readiness, owned PENDING attempt/payment/booking, frozen matching money and eligible planning/expiry. Responses are private, no-store.
- Ineligible first preparation: 409 with no new attempt/effect. Terminal existing replay: 201 UNAVAILABLE with original identity/no POST. Missing planning/readiness/idempotency/provider configuration: 503. Fingerprint conflict: 409. Unsafe Checkout: 409; ownership: 404. Historical rejected replay can return SUPERSEDED_ATTEMPT.
- After confirmed cancellation B uses fresh stable IDs/context/quote/current slot. Refund completion is not a prerequisite. SUBMITTED/PROCESSING/CONFIRMING/FAILED remains distinct from verified COMPLETED; no fixed credit-time promise.

Exact frontend behavior/reason vocabulary: [Payment preparation API](PAYMENT_PREPARATION_API.md).
Frontend repository remains unchanged. Status GET only projects evidence; customer retry/verify
again uses bounded, serialized preparation with the retained key even when the read action is WAIT.
Only START/RESUME enables fresh Checkout GET and native handoff. Persist cancellation/B identities before commands and
exactly replay lost responses; no timeout, callback or countdown declares financial finality.

## Migration and historical recovery

`0023_payment_checkout_readiness` follows `0022_financial_review_recovery`: nullable timestamptz
checkout_ready_until; observation_version integer default 0; order_creation_pending boolean default
false. Existing rows gain no Checkout permission or invented first-POST/account history. Three
DB triggers freeze Payment amount/currency, request quote/currency and item insert/update/delete
once any attempt exists. Cancellation/status/expiry and financial history keep their authorized
transitions. Maintenance writers use Payment-first ordering; unsupported conflicting raw writes
may abort/roll back on DB locking. No item-edit endpoint is added.

Downgrade removes permission fields/guards, not money/provider history. T19 compares full populated
booking/payment/attempt/charge/event/refund/obligation/outbox rows across downgrade to 0022 and
upgrade to head (excluding the three intentionally removed permission fields); verifies guards,
full type metadata and new attempt defaults. No blind historical status repair is necessary:
existing bounded leased reconciliation selects FAILED known Orders/null next_check_at and changes
state only from authenticated evidence. No production backfill/account guess was run.

## Required 38 scenarios

| # | Owner scenario | Exact tests below |
|---|---|---|
| 1 | One original local/remote identity | T1, T20 |
| 2 | Failed instrument resumes identical Order | T1 (failed), T2 |
| 3 | Same/fresh key converge | T1, T2, T20, T21 |
| 4 | Multiple failed instruments no second Order | T2 |
| 5 | Authorized/processing blocks reopen | T1 (authorized/created), T8, T16 |
| 6 | Unknown/incomplete inquiry fails closed | T3 |
| 7 | Lost creation response stable receipt | T14, T21 |
| 8 | Genuine pre-Order rejection distinct | T13 |
| 9 | Concurrent preparation no duplicate | T20, T21 |
| 10 | Account rotation/mismatch ownership | T7 |
| 11 | Pending/uncertain cancellation retains exposure | T4, T6, T10 |
| 12 | Cancelled replay no POST | T4 (cancelled) |
| 13 | Expiry/cutoff/freeze replay no POST | T4, T5 |
| 14 | In-flight creation retained after cancellation | T6 (during POST) |
| 15 | Old Checkout no fresh backend handoff | T4, T6, T16 |
| 16 | Both capture/cancellation winners | T22 (both orderings) |
| 17 | Cutoff/frozen cancellation refused | T23, T24 |
| 18 | Cancel A, fresh B/context, pay B | T10 |
| 19 | B accepted while A unresolved | T10 (a_unresolved) |
| 20 | Capture A before B independent | T10 (a_first) |
| 21 | Capture B before A independent | T10 (b_first) |
| 22 | Late A stays cancelled/compensates | T9 (captured), T10, T25 |
| 23 | Two A captures compensated independently | T25, T28 |
| 24 | Duplicate A capture one economic effect | T25, T28, T29 |
| 25 | Lost cancel response exact replay | T10, T26 |
| 26 | Lost B response exact replay, same quote once | T10 |
| 27 | Slot lost refuses B, A cancelled | T11 |
| 28 | FAILED/known Order/null due discoverable | T9, T12 |
| 29 | Old capture without webhook discovered | T9, T31 |
| 30 | Missing webhook/broker replay/restart converge | T9, T14, T29, T30, T38 |
| 31 | Charge/refund/outbox atomic | T26, T27, T30 |
| 32 | Uncertain/failed refund no false payout/duplicate | T32–T35, T41 |
| 33 | Independent refund confirmation leaves B intact | T10 (a_first/b_first) |
| 34 | Actions/retry same Order versus uncertainty | T1, T7, T13, T17 |
| 35 | Cancelled money receipt is not accepted | T9, T10, T25 |
| 36 | Refund processing versus completion | T10, T36 |
| 37 | No sensitive customer DTO details | T1, T7, T26; strict response models |
| 38 | New observation fences prepare/Checkout | T8, T16 |

## Executed validation

All commands used `.venv\Scripts\python.exe` in the repository. Pytest used
`TIRODHAN_TEST_DATABASE_URL` for disposable local PostgreSQL database `tirodhan_batch_a_test`;
Alembic used the same database via `TIRODHAN_DATABASE_URL`. No production credentials/settings.

| Executed command/check | Actual final result |
|---|---|
| `python -m pytest -q --tb=short` (full suite, explicit PostgreSQL URL) | **1094 passed, 1 skipped, 4 warnings in 556.81s (9:16)**; includes all relevant financial integrations and migration roundtrips |
| `python -m pytest -q tests/integration/test_payment_preparation.py tests/integration/test_checkout_handoff.py tests/integration/test_customer_mobile_core.py --tb=short` | **72 passed in 61.64s**, after correcting the Checkout races/fixture; final full run also covers later cache predicate and migration proof |
| `python -m pytest -q tests/integration/test_payment_preparation.py::test_readiness_migration_preserves_populated_financial_history_and_schema --tb=short` | **1 passed in 4.15s**, populated downgrade/upgrade, triggers, full type comparison and strict attempt defaults; also passed in final full suite |
| `python -m ruff check .` | **All checks passed** |
| `python -m ruff format --check .` | **304 files already formatted** |
| `python -m mypy` | **Success: no issues in 152 source files** |
| `python -m alembic current` and `python -m alembic heads` | Both identify single head **0023_payment_checkout_readiness** |
| `python -m alembic check` | **Exit 1, qualified baseline failure**: exactly the three existing differences below; no new differences |
| `git diff --check` | **Passed**; final staged diff also checked before commit |

Skip: `tests/unit/test_deployment_job.py:99`, POSIX signal exit-status case inapplicable on
Windows. Four warnings are existing FCM `MulticastMessage.tokens` deprecations in dispatch tests.
They are not PostgreSQL integration skips or new payment warnings.

Raw Alembic differences remain: extension-owned `spatial_ref_sys`,
`ix_collection_request_pickup_location_gist` and `ix_collection_request_planning_batch_id`.
The indexes are already defined in migration 0007; none was removed or added to permanent ignore
rules. T19 asserts the exact raw difference set (three and only three), giving **zero new schema
differences**. It additionally enables server-default comparison for all PaymentAttempt fields
and asserts **zero differences**, and checks all three installed initiation-term triggers.
Its populated history comparison includes original full rows, not only table counts.

Actual disposable PostgreSQL integrations run with the explicit test URL; no database tests are
skipped for absent configuration. An earlier full run exposed two real Checkout isolation races
(capture/auth between initial/final reads) and an old paid-item-mutating fixture. READ COMMITTED
handoff and declaring fixture items before initiation fixed these; focused regression: 72 passed.
No failed earlier run is represented as final success. A global server-default diagnostic also
found the pre-existing rider_availability.version default omission (migration 0008 versus baseline
ORM); established full type comparison and stricter payment_attempt default comparison are tested.

Terraform untouched: validation/apply not required or executed. No live merchant/device test,
production repair, real payment or cloud deployment was performed.

## Provider/Operations prerequisites and unresolved work

- Set a short explicit 1..60-second readiness TTL in the target environment; unset disables preparation. Required planning/command/provider/reconciliation configuration remains enforced. Readiness expiry never proves failure.
- Verify persisted merchant identity, capture mode, read permissions and snapshot completeness/visibility for the actual merchant. Same verified account allows key rotation. Legacy unbound records need audited Operations verification; no active-account guess.
- Operate existing webhook queue, inbox/outbox, reconciliation/inventory, refund execution/inquiry, backlog/DLQ monitoring and named financial responder. Missing GET results/age are not finality; held balances stay visible.
- Preserve `razorpay_refund_replay_window_seconds=None` and `razorpay_normal_refund_failure_finality_confirmed=False` defaults. Merchant-specific guarantees and owner approval are required before enabling those safeguards' recovery paths. Automatic normal compensation remains subject to existing hold/reservation checks and verified payout.
- Separate mobile work must explain approved temporary double debit/refund processing before B payment and implement bounded, jittered foreground refresh with manual recovery after its window. No timer-driven financial failure, continuous background polling or fixed bank-credit SLA.
- Independent adversarial review, PR/merge and deployment authorization remain separate gates. Local implementation blockers/owner decisions: none remaining in the authorized local implementation. P1-2/P2-6 remain outside authorized scope.

Verified primary sources: [Order lifecycle](https://razorpay.com/docs/payments/orders/),
[Order facts](https://razorpay.com/docs/api/orders/fetch-with-id/),
[Order instruments](https://razorpay.com/docs/api/orders/fetch-payments/),
[creation/receipt reuse](https://razorpay.com/docs/api/orders/create/).
These do not promise linearizable visibility, bank-credit deadlines or this merchant's refund
replay/finality guarantees. Mock HTTP/PostgreSQL proof is not live-provider proof.

## Exact source and test evidence

### Source

- **C1:** [`src/tirodhan/modules/payments/service.py:150`](../src/tirodhan/modules/payments/service.py#L150) — `initiate_payment_attempt`.
- **C2:** [`src/tirodhan/modules/payments/service.py:450`](../src/tirodhan/modules/payments/service.py#L450) — `process_authenticated_payment_event`.
- **C3:** [`src/tirodhan/modules/payments/razorpay.py:223`](../src/tirodhan/modules/payments/razorpay.py#L223) — `prepare_order`.
- **C4:** [`src/tirodhan/modules/payments/razorpay.py:605`](../src/tirodhan/modules/payments/razorpay.py#L605) — `initiate_payment`.
- **C5:** [`src/tirodhan/modules/payments/reconciliation.py:309`](../src/tirodhan/modules/payments/reconciliation.py#L309) — `reconcile_batch`.
- **C6:** [`src/tirodhan/modules/payments/reconciliation.py:143`](../src/tirodhan/modules/payments/reconciliation.py#L143) — `inquire_target`.
- **C7:** [`src/tirodhan/modules/payments/service.py:94`](../src/tirodhan/modules/payments/service.py#L94) — `_locked_payment_booking`.
- **C8:** [`src/tirodhan/modules/payments/service.py:124`](../src/tirodhan/modules/payments/service.py#L124) — `preparation_eligible`.
- **C9:** [`src/tirodhan/api/routes/checkout.py:57`](../src/tirodhan/api/routes/checkout.py#L57) — `get_checkout`.
- **C10:** [`src/tirodhan/modules/payments/preparation.py:37`](../src/tirodhan/modules/payments/preparation.py#L37) — `payment_action`.
- **C11:** [`src/tirodhan/modules/customer_reads/projections.py:26`](../src/tirodhan/modules/customer_reads/projections.py#L26) — `payment_projection`.
- **C12:** [`src/tirodhan/api/routes/payments.py:89`](../src/tirodhan/api/routes/payments.py#L89) — `post_payment_attempt`.
- **C13:** [`src/tirodhan/modules/payments/refunds.py:87`](../src/tirodhan/modules/payments/refunds.py#L87) — `ensure_cancellation_refund`.
- **C14:** [`src/tirodhan/modules/payments/accounting.py:334`](../src/tirodhan/modules/payments/accounting.py#L334) — `compensate_charge`.
- **C15:** [`src/tirodhan/modules/payments/refunds.py:369`](../src/tirodhan/modules/payments/refunds.py#L369) — `execute_refund_provider_call`.
- **C16:** [`src/tirodhan/modules/collection_requests/service.py:248`](../src/tirodhan/modules/collection_requests/service.py#L248) — `create_collection_request`.
- **C17:** [`src/tirodhan/modules/collection_requests/cancellation.py:38`](../src/tirodhan/modules/collection_requests/cancellation.py#L38) — `cancel_collection_request_by_customer`.
- **C18:** [`migrations/versions/0023_payment_checkout_readiness.py:12`](../migrations/versions/0023_payment_checkout_readiness.py#L12) — `upgrade`.
- **C19:** [`src/tirodhan/api/routes/checkout.py:28`](../src/tirodhan/api/routes/checkout.py#L28) — `checkout_session`.
- **C20:** [`src/tirodhan/modules/payments/models.py:65`](../src/tirodhan/modules/payments/models.py#L65) — `PaymentAttempt`.

### Tests

- **T1:** [`tests/integration/test_payment_preparation.py:111`](../tests/integration/test_payment_preparation.py#L111) — `test_same_order_preparation_action_and_instrument_evidence`.
- **T2:** [`tests/integration/test_payment_preparation.py:165`](../tests/integration/test_payment_preparation.py#L165) — `test_many_failed_instruments_reuse_identity_and_preserve_each_observation`.
- **T3:** [`tests/integration/test_payment_preparation.py:206`](../tests/integration/test_payment_preparation.py#L206) — `test_incomplete_readiness_waits_without_another_order`.
- **T4:** [`tests/integration/test_payment_preparation.py:249`](../tests/integration/test_payment_preparation.py#L249) — `test_uncertain_terminal_replay_never_posts`.
- **T5:** [`tests/integration/test_payment_preparation.py:302`](../tests/integration/test_payment_preparation.py#L302) — `test_first_initiation_checks_planning_before_remote_creation`.
- **T6:** [`tests/integration/test_payment_preparation.py:334`](../tests/integration/test_payment_preparation.py#L334) — `test_cancellation_during_creation_rechecks_post_permission_and_retains_inflight_result`.
- **T7:** [`tests/integration/test_payment_preparation.py:386`](../tests/integration/test_payment_preparation.py#L386) — `test_account_rotation_and_legacy_ownership_fail_closed`.
- **T8:** [`tests/integration/test_payment_preparation.py:426`](../tests/integration/test_payment_preparation.py#L426) — `test_observation_during_and_after_preparation_fences_handoff`.
- **T9:** [`tests/integration/test_payment_preparation.py:472`](../tests/integration/test_payment_preparation.py#L472) — `test_legacy_failed_established_order_is_discovered_without_new_post`.
- **T10:** [`tests/integration/test_payment_preparation.py:523`](../tests/integration/test_payment_preparation.py#L523) — `test_cancel_replace_independent_accounting_and_verified_refund`.
- **T11:** [`tests/integration/test_payment_preparation.py:615`](../tests/integration/test_payment_preparation.py#L615) — `test_cancel_replace_requires_new_context_and_current_slot`.
- **T12:** [`tests/integration/test_payment_preparation.py:639`](../tests/integration/test_payment_preparation.py#L639) — `test_historical_missing_account_gets_bounded_attention_without_guessing`.
- **T13:** [`tests/integration/test_payment_preparation.py:663`](../tests/integration/test_payment_preparation.py#L663) — `test_positive_initial_order_rejection_is_distinct_from_instrument_failure`.
- **T14:** [`tests/integration/test_payment_preparation.py:712`](../tests/integration/test_payment_preparation.py#L712) — `test_restart_before_or_after_post_recovers_one_intent`.
- **T15:** [`tests/integration/test_payment_preparation.py:752`](../tests/integration/test_payment_preparation.py#L752) — `test_financial_terms_are_frozen_at_first_durable_attempt`.
- **T16:** [`tests/integration/test_payment_preparation.py:808`](../tests/integration/test_payment_preparation.py#L808) — `test_committed_observation_during_checkout_read_revokes_handoff`.
- **T17:** [`tests/integration/test_payment_preparation.py:849`](../tests/integration/test_payment_preparation.py#L849) — `test_readiness_expiry_is_permission_expiry_and_unset_policy_fails_closed`.
- **T18:** [`tests/integration/test_payment_preparation.py:889`](../tests/integration/test_payment_preparation.py#L889) — `test_documented_order_response_without_creation_option_can_start_checkout`.
- **T19:** [`tests/integration/test_payment_preparation.py:915`](../tests/integration/test_payment_preparation.py#L915) — `test_readiness_migration_preserves_populated_financial_history_and_schema`.
- **T20:** [`tests/integration/test_razorpay_payments.py:94`](../tests/integration/test_razorpay_payments.py#L94) — `test_concurrent_initial_lookups_share_one_preparation`.
- **T21:** [`tests/integration/test_razorpay_payments.py:59`](../tests/integration/test_razorpay_payments.py#L59) — `test_timeout_after_remote_order_commit_recovery_and_concurrent_replay_converge`.
- **T22:** [`tests/integration/test_customer_financial_cancellation.py:178`](../tests/integration/test_customer_financial_cancellation.py#L178) — `test_real_lock_capture_cancellation_race_has_one_compensation_without_deadlock`.
- **T23:** [`tests/integration/test_customer_financial_cancellation.py:243`](../tests/integration/test_customer_financial_cancellation.py#L243) — `test_real_planning_cancellation_race_revalidates_after_wait`.
- **T24:** [`tests/integration/test_customer_financial_cancellation.py:918`](../tests/integration/test_customer_financial_cancellation.py#L918) — `test_cancellation_api_authoritative_refusal_and_safe_error_envelope`.
- **T25:** [`tests/integration/test_customer_financial_cancellation.py:702`](../tests/integration/test_customer_financial_cancellation.py#L702) — `test_additional_distinct_charge_preserves_reconciliation_facts_and_never_reopens`.
- **T26:** [`tests/integration/test_customer_financial_cancellation.py:94`](../tests/integration/test_customer_financial_cancellation.py#L94) — `test_cancel_api_atomic_compensation_replay_and_matching_reads`.
- **T27:** [`tests/integration/test_customer_financial_cancellation.py:760`](../tests/integration/test_customer_financial_cancellation.py#L760) — `test_compensation_failure_rolls_back_late_capture_and_retries_once`.
- **T28:** [`tests/integration/test_financial_reconciliation.py:278`](../tests/integration/test_financial_reconciliation.py#L278) — `test_additional_charge_duplicate_events_and_multi_charge_cancellation`.
- **T29:** [`tests/integration/test_financial_correctness_closure.py:93`](../tests/integration/test_financial_correctness_closure.py#L93) — `test_queue_lost_ack_replay_commits_one_acceptance_and_inbox`.
- **T30:** [`tests/integration/test_financial_correctness_closure.py:115`](../tests/integration/test_financial_correctness_closure.py#L115) — `test_queue_failure_before_commit_rolls_back_evidence_inbox_and_money`.
- **T31:** [`tests/integration/test_financial_review_closure.py:305`](../tests/integration/test_financial_review_closure.py#L305) — `test_retired_window_late_capture_is_found_by_bounded_history`.
- **T32:** [`tests/integration/test_financial_reconciliation.py:312`](../tests/integration/test_financial_reconciliation.py#L312) — `test_refund_missing_webhook_and_uncertain_initiation_reconciles_same_operation`.
- **T33:** [`tests/integration/test_financial_reconciliation.py:474`](../tests/integration/test_financial_reconciliation.py#L474) — `test_failed_status_without_confirmed_finality_never_releases_obligation`.
- **T34:** [`tests/integration/test_financial_review_closure.py:329`](../tests/integration/test_financial_review_closure.py#L329) — `test_hold_after_claim_commits_before_post_is_rechecked`.
- **T35:** [`tests/integration/test_financial_review_closure.py:135`](../tests/integration/test_financial_review_closure.py#L135) — `test_refund_contradiction_holds_existing_execution`.
- **T36:** [`tests/integration/test_customer_financial_cancellation.py:453`](../tests/integration/test_customer_financial_cancellation.py#L453) — `test_refund_read_real_lifecycle`.
- **T37:** [`tests/integration/test_financial_reconciliation.py:203`](../tests/integration/test_financial_reconciliation.py#L203) — `test_five_minute_ui_timeout_is_not_financial_failure`.
- **T38:** [`tests/integration/test_financial_reconciliation.py:684`](../tests/integration/test_financial_reconciliation.py#L684) — `test_expired_reconciliation_claim_is_recovered_without_waiting_for_memory`.
- **T39:** [`tests/unit/test_config.py:8`](../tests/unit/test_config.py#L8) — `test_checkout_readiness_is_bounded_permission`.
- **T40:** [`tests/unit/test_config.py:14`](../tests/unit/test_config.py#L14) — `test_checkout_readiness_configuration_is_explicit`.
- **T41:** [`tests/integration/test_financial_correctness_closure.py:490`](../tests/integration/test_financial_correctness_closure.py#L490) — `test_external_refund_before_extra_capture_blocks_payout_without_losing_capture`.



## Changed files

- `.env.example`
- `docs/ARCHITECTURE.md`
- `docs/DOMAIN_MODEL.md`
- `docs/IDEMPOTENCY.md`
- `docs/PAYMENT_P1_1_IMPLEMENTATION_REPORT.md`
- `docs/PAYMENT_PREPARATION_API.md`
- `docs/PAYMENT_RECONCILIATION_AND_MOBILE_STATUS_EVENTS.md`
- `docs/PHASE_2_FINANCIAL_MERGE_RUNBOOK.md`
- `docs/SCHEMA_DESIGN.md`
- `docs/adr/ADR-005-payments-refunds.md`
- `migrations/versions/0023_payment_checkout_readiness.py`
- `src/tirodhan/api/routes/checkout.py`
- `src/tirodhan/api/routes/customer_finance.py`
- `src/tirodhan/api/routes/customer_reads.py`
- `src/tirodhan/api/routes/payments.py`
- `src/tirodhan/core/config.py`
- `src/tirodhan/modules/customer_reads/projections.py`
- `src/tirodhan/modules/customer_reads/repository.py`
- `src/tirodhan/modules/customer_reads/schemas.py`
- `src/tirodhan/modules/payments/models.py`
- `src/tirodhan/modules/payments/ports.py`
- `src/tirodhan/modules/payments/preparation.py`
- `src/tirodhan/modules/payments/razorpay.py`
- `src/tirodhan/modules/payments/reconciliation.py`
- `src/tirodhan/modules/payments/service.py`
- `tests/integration/razorpay_helpers.py`
- `tests/integration/test_checkout_handoff.py`
- `tests/integration/test_collection_payment.py`
- `tests/integration/test_customer_financial_cancellation.py`
- `tests/integration/test_customer_mobile_core.py`
- `tests/integration/test_financial_reconciliation.py`
- `tests/integration/test_financial_review_closure.py`
- `tests/integration/test_payment_preparation.py`
- `tests/integration/test_razorpay_payments.py`
- `tests/unit/test_config.py`


## Follow-up: Payment term freeze versus first-attempt FK concurrency

Review baseline: `b04fad8964c705430c7b3a2c5c757d6d57015caf`; same dedicated branch.
The original A16 proof covered already-established attempts and application Payment-first
initiation, but missed a direct database two-writer race. The existing application initiation
already explicitly locks Payment FOR UPDATE; the gap was the trigger's standalone DB protection.

**Reproduced vulnerability:** a non-key Payment amount/currency UPDATE takes FOR NO KEY UPDATE,
compatible with the first PaymentAttempt INSERT's real FK FOR KEY SHARE. The trigger's EXISTS
check cannot see an uncommitted concurrent attempt. With two distinct PostgreSQL sessions at
READ COMMITTED, both transaction orderings allowed the attempt to commit and then the changed
terms transaction to commit. Before correction, all four commit cases failed the new regression:
amount became 600 or currency USD, with one durable first attempt. No provider calls were made.

**Smallest correction:** the Payment branch of enforce_initiated_booking_terms upgrades the
same Payment row to FOR UPDATE before checking attempts. This conflicts with FK KEY SHARE and
uses the existing Payment-first ancestor lock; no Attempt row lock, status/refund change or new
trigger/table is introduced. Identical-value updates retain their original early return.
Under the application's READ COMMITTED write isolation, a waiting update rechecks newly committed
attempts and is rejected. An update that wins finishes before the first attempt can commit.

[Regression test](../tests/integration/test_initiated_payment_terms_concurrency.py):
`test_first_attempt_and_terms_update_serialize` uses two real backend PIDs, raw non-key UPDATE
and raw INSERT/FK enforcement without API pre-locks. It observes pg_blocking_pids to prove waits
rather than assuming a sleep means blocking. Eight variants cover amount/currency, each writer
first, and first-writer commit/rollback; final committed values/counts, identical-value replay and
later changed-value rejection are asserted. The tests explicitly use READ COMMITTED, matching
application writes; repeatable-read customer projections remain read-only and untouched.

Executed before correction:
`python -m pytest -q tests/integration/test_initiated_payment_terms_concurrency.py -k True --tb=short`
→ **4 failed, 4 deselected in 13.85s**, each proving both transactions committed unsafely.
Executed after correction:
`python -m pytest -q tests/integration/test_initiated_payment_terms_concurrency.py --tb=short`
→ **8 passed in 13.68s**.
Broader focused command:
`python -m pytest -q tests/integration/test_initiated_payment_terms_concurrency.py tests/integration/test_payment_preparation.py tests/integration/test_customer_financial_cancellation.py --tb=short`
→ **104 passed in 133.13s**, including populated migration roundtrip/schema alignment,
preparation, cancellation, term freezes and all eight concurrency variants.
`python -m ruff check .` passed; `python -m ruff format --check .` reported **305 files already
formatted**; `python -m mypy` passed **152 source files**; diff checks passed.
The earlier 1094-test full-suite result above belongs to the baseline delivery; the complete
full suite was not repeated for this three-line migration correction. All commands used the
same explicitly configured disposable local PostgreSQL database and repository virtualenv.

Corrective scope: only `migrations/versions/0023_payment_checkout_readiness.py`,
`tests/integration/test_initiated_payment_terms_concurrency.py`, and this report appendix.

This remains an unmerged/unreleased migration correction in place. The already-applied disposable
0023 was downgraded to 0022 and upgraded to reinstall the corrected function; both succeeded.
Changing a migration file does not update a database already stamped 0023. No production/nonprod
migration or data repair was performed, and this disposable reinstall is not rollout advice.
No unrelated payment/refund functionality, PR, merge or deployment is part of the correction.

PostgreSQL's [row-lock conflict rules](https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS)
confirm the compatible weaker locks and the stronger FOR UPDATE conflict; the executable test
establishes their consequence in this repository, including the post-wait trigger recheck.
