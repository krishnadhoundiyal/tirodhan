# Phase 2 financial review findings closure

Repository: `C:\Users\91956\Tirodhan\tirodhan`. Branch: `phase2/customer-financial-cancellation`.
Starting commit: `7c0dc0b91e3f36aa6c483b56c7097b66bb3f4e0b`. Review: 2026-10-10 UTC / 2026-10-11 IST.
Scope: the preceding adversarial review's A1–A10, financial risks, stuck states and Q1–Q15.
This document is the comprehensive closure register; earlier reports remain historical evidence.

Local corrections preserve the approved payment/charge/refund/obligation model, Payment-first
transactions, native original-operation identity, Azure Service Bus and the existing log sink.
There is no new testing framework, paid service, frontend change or commercial policy.
All provider traffic used in tests is synthetic HTTP; PostgreSQL tests use the disposable local
database. No authenticated Razorpay request, real payout, deployment, push or PR was performed.

`FIXED AND TESTED` and `PROVEN SAFE — NO CHANGE REQUIRED` describe local evidence for the stated
invariant. `PROVIDER BLOCKED — SAFE GUARD VERIFIED`, `OPERATIONAL ENABLEMENT PENDING`,
`OWNER DECISION REQUIRED` and `NOT CLOSED` are **not production-ready closure**. A passing local
test cannot establish a merchant contract, human response SLA or distributed transaction.

Test references: **N** = `tests/integration/test_financial_review_closure.py`; **F** =
`test_financial_reconciliation.py`; **C** = `test_financial_correctness_closure.py`; **R** =
`test_razorpay_refunds.py`; **U** = `tests/unit/test_financial_provider_boundaries.py`.
Named scenarios below refer to these actual tests, not newly invented test frameworks.

## Comprehensive findings register

B and S identifiers enumerate the preceding review's unnumbered risk and stuck-state sections.
S3/S4 separate its combined claimed-but-unsent/DLQ scenario so neither dependency is hidden.
The 54 entries contain 19 FIXED AND TESTED, two PROVEN SAFE — NO CHANGE REQUIRED,
23 PROVIDER BLOCKED — SAFE GUARD VERIFIED, seven OPERATIONAL ENABLEMENT PENDING,
three OWNER DECISION REQUIRED and zero NOT CLOSED. The 33 external, operational and owner
dependencies remain release gates; they are not counted as production-ready closure.

| Finding ID | Original risk | Reproduction | Correction / evidence | Test result | Status | Remaining dependency |
|---|---|---|---|---|---|---|
| A1 | Individual failed payment terminalizes unresolved order; authorized hook ignored | N failed-webhook test failed before fix: FAILED instead of PENDING | Individual webhook/inventory failures retain PENDING and due inquiry; authorized is SUBMITTED. Only complete order inquiry establishes order failure | N failed webhook + F unresolved order/retry tests | FIXED AND TESTED | Order inventory guarantees: Q8 |
| A2 | Approval resolves cases and hides unpaid liability | N approval test failed before fix: no OPEN case | Approval records authorization/status signal, retains debt/cases; Operations includes open obligations, held and uncertain refunds. Cases close only against verified paid balance | N approval; F historical/expiry/manager replay | FIXED AND TESTED | Actual provider completion remains required |
| A3 | Recurrent cause returns resolved case without attention | N recurrence failed before fix | Same case key reopens with occurrence/detection time; occurrence-specific attention. New contradiction updates cause FK even while OPEN | N recurrence, concurrent investigation; aging traversal test | FIXED AND TESTED | Human escalation enablement: A10 |
| A4 | Financial holds have no controlled release | Before correction, source had no audited release command; final added variant reproduced a hold deadlock for a locally unclaimed original | Manager resolve requires fresh matching outcome/report, complete exact inventory of submitted operations, unchanged occurrence/cause, current assessment, live role and no other charge hold. A locally unclaimed PENDING intent remains debt and can resume original execution after the mechanical hold clears; uncertain claimed originals still require provider identity. Case resolution reports remaining holds honestly | N concurrent refund/settlement release; newer contradiction, other hold and revoked-role races; unclaimed success vs claimed blocker | FIXED AND TESTED | Claimed originals with unknown identity require Q5 provider evidence. Dispute, external credit and adjustment commercial disposition remains O1; no override provided |
| A5 | Contradictory refund facts do not consistently hold execution | N contradiction failed before fix: flag false | Amount/currency/payment/account/refund-ID contradictions hold obligation; worker also checks typed open cases for legacy rows. Revalidates after GETs before POST. Legacy merchant binding uses Attempt when Charge absent | N contradiction/hold race/legacy account variants; C conflicting payload/dispute cases | FIXED AND TESTED | External race after final local check: B2/B11 |
| A6 | Verified processed legacy refund remains FAILED | N legacy outcome failed before fix | Common outcome processor records SUCCEEDED/completion and preserves contradictory finality evidence. No new refund/key is created | N legacy truth; R updated legacy lifecycle; F late success after replacement | FIXED AND TESTED | Provider finality Q1–Q2 remains unconfirmed by default |
| A7 | Unknown settlement movements lack investigation/disposition provenance | Source only retained unmatched evidence; no standalone audit target | Fresh bounded report investigation returns verified match, retained provider blocker or owner review. Nullable Payment audit has real evidence FK; global audit traversal works before mapping. Unknown movements never fabricate a Payment or Refund | N unmapped investigation/replay/immutable audit/downgrade refusal; C unknown report evidence | FIXED AND TESTED | Q14 access/completeness and O1 commercial disposition |
| A8 | First 100 children truncate financial evidence | Original detail used fixed limit 100 without continuation; source demonstration, not a claimed pre-fix test run | Independent UUID cursors for eight sections; obligations query all charges; global provider/settlement/audit/checkpoint/delivery queues. Important identities, proof/time/hold fields retained | N 111 cases traversed in 100+11 without duplication; standalone audit traversal | FIXED AND TESTED | Cursor traversal is live data, not a frozen snapshot; refresh open queues |
| A9 | Rotated secret rejects legitimate older retries | N retiring-secret test failed before fix | Current plus optional previous secret with explicit aware deadline; HMAC over exact bytes, bounded overlap, no signature bypass; optional Key Vault binding | N current/retiring/expired/tampered signature tests | FIXED AND TESTED | Merchant overlap timing and deployment: Q10/O2 |
| A10 | Exception outbox is never routed; no aging attention or usable queue | N outbox test failed before route: 0 published | Identifier-only attention routes to approved JSON log path. Bounded repeated aging and manager exception/debt/checkpoint/delivery/audit queues exist; records stay actionable without a reliable push | N log sink and repeated aging; F identifier-only status signals; publisher regression | OPERATIONAL ENABLEMENT PENDING | Deploy schedules/log shipping, assign responders, prove SLA/alerts/acknowledgement; no human notification delivery is claimed |
| B1 | Hold commits after claim, before refund POST | N independent-session test pauses GET, commits contradiction, resumes worker | Final Payment-first hold check prevents the still-avoidable POST; legacy cases also gate execution | N hold_after_claim test: zero external refunds/POSTs | FIXED AND TESTED | No atomic guarantee for a hold arriving after final local check; B2/B11 |
| B2 | Dashboard refund races approval/execution | N pauses inventory GET, inserts external payout during each flow | Approval refuses; execution retains unknown movement/hold and refuses POST. Later inventory retains external facts. Entire preflight-to-provider interval cannot be locked by PostgreSQL | N two Dashboard variants, C Dashboard/before-capture and F external-refund guards | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Q9 visibility + Q13 chargeback semantics; Operations must control Dashboard payout permissions/concurrency |
| B3 | Capture after creation-time window retired is missed | N original forward scan advances; late old capture remains missing. Historical argument absent before correction | Separate bounded cyclic payment/refund history lanes use existing verified processor; settlement history also cycles | N retired-window test finds second actual charge, one approved compensation, no second acceptance | FIXED AND TESTED | Historical start must include merchant history; cycle throughput/rates Q8/Q12 |
| B4 | Completed unmatched transport never runs after mapping appears | N failed before fix: only one charge after mapping | Existing targeted reconciliation selects now-mapped unmatched capture references, performs fresh provider GET, then marks retained events against exactly matching verified charges. No stored fact is replayed as new monetary authority | N completed delivery: two distinct charges, one compensation, repeated batch unchanged | FIXED AND TESTED | Historical account/credential availability Q10 |
| B5 | Claim persisted but original POST never reached provider | N synthetic claimed record; GET absent | Unknown absence never resets original submission time. Audited original recovery queues same Refund only in confirmed retention or fresh unclaimed state; worker repeats guards | N original identity: default blocked; synthetic confirmed window produces one operation, no replacement | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Q4/Q5; actual guaranteed retention or provider proof of original nonpayability |
| B6 | Legacy null-charge row bypasses merchant rotation check | N known original account with different/new credentials | Charge account, or retained Attempt account for legacy, gates POST. Same stable merchant with rotated key remains usable; different merchant blocks execution and contradictory outcome | N legacy null-charge account variants; C legacy key/history/reservation tests | FIXED AND TESTED | Historical rows without provable merchant binding need Q10 confirmation; never guess a binding |
| B7 | Failed hook then fresh nonpayable GET/pending/success races | F already proves fresh API failure metadata on FAILED and late pending/success contradiction | Existing processor attaches verified failure proof; later payable truth clears proof, holds and prevents replacement execution. No assumed terminality from webhook | F failed-finality, concurrent replacement/late success, pending-after-failure; N retention blocker variants | PROVEN SAFE — NO CHANGE REQUIRED | Real merchant guarantees Q1–Q2 |
| B8 | Recurrence/manager concurrency loses attention | N recurrence and independent-session cause change | Stable case/occurrence and current cause checks prevent stale release; duplicate command commits one audit | N recurrence and three investigation race variants | FIXED AND TESTED | On-call action A10 |
| B9 | Old MATCHED or repeated old facts defeat current mismatch | N A→held→A test failed before fix | Latest immutable assessment governs missing check; per-movement serialization and predecessor-bound fingerprint append return-to-prior-facts assessment | N current settlement cycle; C fee rule/replay tests | FIXED AND TESTED | Q14 report chronology/completeness |
| B10 | Poison/stale checkpoints suppress unrelated financial work | N one inventory lane returns 429 while another returns valid evidence | Failed scan preserves offset, records safe last-error/check time and releases/backoffs claim; inventory lanes remain independent. Report failures do not suppress other configured report days | N two-lane test; C lease takeover, page resumption and insertion between passes | FIXED AND TESTED | Malformed provider report cannot be silently skipped; operator repairs access/report; Q8/Q14 |
| B11 | Replay retention expires during delayed GET/POST; overlapping jobs | N shifts adapter clock at actual HTTP boundary | Original deadline includes configured HTTP budget; checked after GET and immediately before HTTP. Expired/unconfirmed replay never rotates key or replaces debt | N native deadline and original recovery; F overlap and C lease takeover; R original body/key crash variants | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Guaranteed merchant retention and in-flight arrival bound Q4/Q5; local timeout is not provider arrival proof |
| B12 | Operations misses held, unmatched, stale, or >100 evidence | N pagination/standalone audit/debt queue; original source truncation | Complete bounded child/global queues show times, holds, missing mappings, failed scans, durable delivery identities and recovery endpoints | N 111-row traversal/log aging/audit; F manager HTTP RBAC | FIXED AND TESTED | Human workflow/scheduling A10/O2 |
| S1 | Manager never acts on late captured money | F expiry/late capture and N unpaid approval | OPEN charge case and debt inventory persist; aging does not forgive debt or reopen booking | N/F local queue and aging proofs | OPERATIONAL ENABLEMENT PENDING | Assigned manager, SLA and deployed escalation |
| S2 | Refund intent outbox never published | Existing publisher leaves failed sends PENDING | Delivery queue exposes event/refund/times/attempts; repair publisher/identity, rerun finite publisher; original recovery can queue same economic identity | R publisher/consumer recovery; N original recovery | OPERATIONAL ENABLEMENT PENDING | Enable publisher, refund entity and workload identities; inspect actual delivery |
| S3 | Claimed original request never received remotely | Durable PROCESSING/uncertainty without provider ID | Fresh GET followed only by confirmed bounded original replay, or retained blocker; immutable first submission cannot reset | N original-recovery and preflight timeout | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Q4/Q5; refund remains owed/visible |
| S4 | Broker retries exhaust into DLQ | R inbox/crash/abandon and N original recovery | Repair receiver; manager recover-original GETs then queues same Refund with a new transport event, retaining original native identity. Existing DLQ redrive preserves message ID/subject/bytes | R lost-ack/domain replay and N one-operation recovery | OPERATIONAL ENABLEMENT PENDING | Real queue/DLQ inspection, receiver enablement, credentials; aged replay still Q4/Q5 blocked |
| S5 | Known provider refund submitted, hook missing | F scheduled inquiry completes without webhook | GET known provider ID through same processor; queued debt/case until verified completion | F scheduled refund inquiry and missing webhook; C GET recovers one remote operation | PROVEN SAFE — NO CHANGE REQUIRED | Provider GET availability; no completion timer |
| S6 | Unknown refund ID and omitted correlators | U both-correlator scenarios; C unknown Dashboard discovery | Known provider ID is authoritative mapping; unknown ID requires original receipt and UUID note. Unmapped operation retained/held; no amount-only association | U original-correlator and C/N external evidence proofs | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Q7/Q9; provider supplies verifiable original correlation or O1 disposition |
| S7 | GETs time out indefinitely | N preflight timeout + existing inquiry tests | Durable INITIATION_UNCERTAIN, OPEN case/debt, due backoff and immutable manager investigation; no failed/absent inference | N preflight timeout, F polling, C retained aged blockers | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Restore provider/credential availability; Q5 |
| S8 | Refund remains pending indefinitely | F replacement refuses pending original | Reservation remains; age creates attention, never another refund | F replacement-before-nonpayability variants and N aging | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Provider completes or proves irrevocable nonpayability |
| S9 | Reconciliation/inventory jobs disabled | Policy default intentionally requires explicit configuration | Pull Operations queues remain available; unapplied/never-checked checkpoint is visible; no fabricated automated resolution | Policy tests; N bounded queues; Terraform validation | OPERATIONAL ENABLEMENT PENDING | Explicit merchant-tested budgets/schedules/start date and deployed jobs |
| S10 | FAILED but finality unproved | F confirms false default preserves reserve | FAILED debt and case remain visible; manager replacement refused. Later verified success recorded. Technical replay blocker closes only with actual nonpayable proof or success | F finality false; N two retention-proof variants/legacy truth | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Q1/Q2 |
| S11 | Replacement authorized but original later payable | Existing F two-manager and late outcome tests | Exactly one replacement; original payable truth retained, proof cleared, hold blocks still-avoidable replacement; in-flight double credit stays visible as overpaid obligation | F concurrent replacement/late success/pending contradiction | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Q1/Q2 contract and Q13 cross-channel credit; no distributed atomicity claim |
| S12 | Legitimate hold persists forever | N fresh known refund/settlement plus stale cause/role races | Resolve only approved evidenced holds; unrelated holds remain. Original operation may be recovered after actual clearance; disputes/external credits/adjustments remain held | N release, other hold, revoked role and newer contradiction | OWNER DECISION REQUIRED | O1 for commercial/accounting disposition beyond approved mechanical corrections |
| S13 | Legacy FAILED hides actual processed refund | N failed pre-fix; R legacy lifecycle | Verified completion updates same retained Refund and resolves technical failure attention; no new payout | N/R and C immutable history | FIXED AND TESTED | Merchant evidence must remain authentic |
| S14 | Terminal financial result but stale transport/attention remains | R crash/replay; N retention-proven failure variants | Consumer retries domain identity; stale delivery has no new money effect. Obsolete retention blocker closes only with success/nonpayable proof; unpaid failure case remains | R crash/uncertainty/duplicate consumer and N retention cases | FIXED AND TESTED | Broker/log delivery enablement O2 |
| Q1 | Universal normal FAILED finality unconfirmed | Published states do not prove irrevocable nonpayment for every rail | API evidence + explicit merchant finality flag; default false, reserve retained | F false-finality/replacement; N failure proof variants | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks Razorpay: does API normal failed irrevocably preclude credit for every enabled rail? Require written guarantee + test/live examples; replacement remains disabled without proof |
| Q2 | Failed can later credit under same ID | No universal no-later-credit guarantee in reviewed contract | Later payable truth is recorded; failure proof revoked; unsafe replacement held | F late success/pending after replacement; N legacy success | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks: can any previously failed rfnd later process/credit, including bank retries? Require per-rail answer and transition examples; no presumed finality |
| Q3 | Reversed representation ambiguous | FAQ describes nonreversible issued refunds but elsewhere lists reversal among final hook outcomes; entity/hooks lack a usable reversal contract | No invented reversal handler or manual payout override; report errors/movements remain visible | N reversed/unrecognized GET refusal; C typed report retention | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks exact entity value/event/GET/report and economic meaning of reversal for this Gateway product; require payloads + finality semantics |
| Q4 | Native key retention duration/scope absent | Native documentation states key/body reuse, omits retention bound | Retention window unset; aged recovery refuses POST | C aged window, N original/default/deadline, R same key/body | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks guaranteed X-Refund-Idempotency duration/scope, clock origin, key rotation/test/live effects. Require written minimum; replay remains disabled until explicitly configured |
| Q5 | Long outage/DLQ delay cannot prove original absence | GET empty is not absence/nonpayability proof | Same durable original identity or retained blocker, never automatic replacement/reset | N original never sent/timeout; C aged recovery; R crash redelivery | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks safe recovery after retention: which verifiable endpoint/evidence proves original never payable? Require authoritative original-operation lookup/finality process; manual investigation until supplied |
| Q6 | Same-key 409 original still processing or body conflict | Native API documents both conflicts | Preserve key/body, uncertain + GET; never adopt generic change-key advice for the same intent | R/U provider 409 handling and native replay | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks endpoint/correlation/retry timing for an in-progress original. Require recoverable same-key example and retention guarantee; no key rotation to bypass conflict |
| Q7 | Receipt/notes omitted or changed | Published samples allow null receipt; no universal presence promise | Known rfnd mapping accepted; unknown identity requires both original correlators; contradictions retained | U both original-correlator tests; R provider-ID/receipt/notes hooks | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks which created/processed/failed/Dashboard/delayed events preserve our note/receipt. Require missing-field samples and authoritative recovery alternative; no amount-only binding |
| Q8 | Offset pagination/order inventory not immutable snapshot | Charge inventories list bounded pagination; exhaustive stable snapshot is unproved | Bounded repeated passes/cross-checks and cyclic history; incomplete reads never establish failure | U 100/101/203 and unstable inventory; C/N retired/resumed window | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks ordering, exhaustive order output above 100, insertion/visibility/snapshot guarantees. Require high-count concurrent pagination evidence; no universal coverage/finality claim |
| Q9 | Dashboard cross-channel visibility/lag unproved | API inventories exist; maximum cross-channel lag unproved | Discover, retain and hold external refunds; preflight GET and actual-charge cap. No fabricated local intent | C external inventory/before-capture; N Dashboard concurrency | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks maximum visibility lag for pending/failed/processed Dashboard refunds in charge/account GET, hooks and recon. Require test/live observations; Operations restricts unsupervised competing payouts |
| Q10 | Merchant binding/historical reads/secret rotation | API authentication + older-secret retry verification documented | Stable configured account, legacy Attempt fallback, bounded previous-secret overlap; wrong merchant refuses payout | N legacy credential and rotation signature cases | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks stable account_id for test/live/key rotation, historical access, retry signing/overlap timing. Require account-specific keys/hooks proof; unproved legacy mappings remain held |
| Q11 | Five-second ack/retries/cold starts not demonstrated live | Published at-least-once, 5s timeout, 24h retry/disable; no event order promise | Existing queue-first awaited ack; polling fallback and bounded trusted ingress; scale-to-zero retained | U broker ack/failure; C queue commit/poison; Terraform | OPERATIONAL ENABLEMENT PENDING | Operations proves subscriptions, reenablement, cold-start/burst p95/p99, broker access and Alert Email Address. No live latency guarantee or implicit min-replica cost increase |
| Q12 | Merchant endpoint/burst quotas unknown | General API docs describe 429, not our numeric quotas | Finite pages/batches, due backoff and explicit policy config; failed checkpoint visibility | U bounded page/429; N independent failed lane | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks sustained/burst limits per payment/refund/recon/dispute endpoint and hook concurrency/Retry-After. Require quotas and load evidence before production schedule |
| Q13 | Dispute deductions/reversals/refund double credit | Documented dispute events/fields; merchant rail effects need proof | Typed minimal dispute retention and payout hold; no guessed win/loss/reversal economics | C dispute-after-refund; F payable original after replacement | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks deduction/reversal/report effects, refund restrictions and prior-refund proof per rail. Require written rule + sample sequences; owner approves O1 disposition |
| Q14 | Recon entitlement, day timezone, lag, membership, fee/net and completeness | Published typed report and count<=1000; merchant guarantees unproved | Account-bound immutable assessments; explicit fee rule or gross-only match; verified due/membership only; old days cycle; unknowns investigated/audited | C report grain/pages/expected missing; N current assessment and standalone investigation | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks test/live access, timezone, entity uniqueness/membership, tax inclusion, holds/due times, max late changes, exhaustive coverage/completion. Require representative reconciled merchant reports; no guessed expected-population producer |
| Q15 | Fresh normal refund over six months unsupported | Native documentation states age restriction | Provider rejection never forgives debt or proves universal replacement finality; failure stays visible | R failed response; F false finality/debt; N approval inventory | PROVIDER BLOCKED — SAFE GUARD VERIFIED | Merchant owner asks calendar boundary/provider timestamp/per-method age and supported alternative. Require account answer; owner chooses O3 alternative disposition; no unsafe repeated payout |
| O1 | External credits, partial refunds, disputes and adjustments lack approved commercial disposition | Existing policy leaves these decisions open; no local Refund can be fabricated for Dashboard evidence | Manager investigation records owner/provider blocker and retains hold/history. Approved mechanical identity/amount/settlement corrections can be released | N audited release vs retained other hold; C unknown/dispute retention | OWNER DECISION REQUIRED | Owner approves explicit credit recognition and dispute/adjustment/partial-refund policy, including any necessary conventional model extension; no implicit ledger/status rewrite |
| O2 | Visibility code is mistaken for deployed response capability | No Azure deployment or real responder acknowledgement in scope | Existing log sink + manager queues + executable recovery commands and runbook below | Local N/F/U/Terraform validation only | OPERATIONAL ENABLEMENT PENDING | Operations deploys migration/app/jobs, secret overlap, log shipping and dashboards; assigns human responders and confirms workload identities/DLQ procedure |
| O3 | Rescheduling or old-payment alternative payout would be silently chosen | Approved historical policy explicitly separates these commercial choices | Late money remains paid but unfulfilled; original request not reaccepted; debt retained when normal refund cannot finish | F expiry/historical/late capture and N unpaid cases | OWNER DECISION REQUIRED | Owner separately approves rescheduling/alternative repayment and any paid provider need; this change selects neither |

## Provider evidence reviewed

Primary Razorpay documentation was reopened on 2026-10-10. Published behavior and missing
merchant guarantees are separate in Q1–Q15. The unanswered questions above are for the merchant
account owner to obtain from Razorpay support; no support message was sent.

The [normal idempotent endpoint](https://razorpay.com/docs/api/refunds/normal-refunds-idempotent/)
documents original key/body reuse and 409 conflicts; it supplies no guaranteed retention
duration. Its six-month restriction does not authorize forgiving debt or changing an original
operation's identity. [Refund entity](https://razorpay.com/docs/api/refunds/entity/) and
[refund FAQs](https://razorpay.com/docs/payments/refunds/faqs/) do not establish universal failed
nonpayability or an implementable reversal contract; that is a limitation of the reviewed
evidence, not a statement that such transitions can never occur.
In particular, the FAQ describes issued refunds as nonreversible but separately includes
reversal among final webhook outcomes. The entity and hook pages list the usual three states
and no specific reversed hook. Q3 requests merchant-specific payloads and economic semantics
to resolve that discrepancy; the published inconsistency is not guessed into code.

[Refund hooks](https://razorpay.com/docs/webhooks/refunds/),
[charge-refund inventory](https://razorpay.com/docs/api/refunds/fetch-multiple-refund-payment/),
[order payments](https://razorpay.com/docs/api/orders/fetch-payments/) and
[account refunds](https://razorpay.com/docs/api/refunds/fetch-all/) establish available interfaces,
not this merchant's worst-case visibility or snapshot guarantees.
[API authentication](https://razorpay.com/docs/api/authentication/) authenticates credentials;
historical merchant binding still needs account confirmation. [Webhook validation](https://razorpay.com/docs/webhooks/validate-test/)
requires verifying older retries with the original secret; the code now supports explicit overlap.

[Webhook best practices](https://razorpay.com/docs/webhooks/best-practices/) explains the delivery
timeout/retry/disable behavior. [API guidance](https://razorpay.com/docs/api/understand/) does not
give this merchant's endpoint quotas. [Dispute hooks](https://razorpay.com/docs/webhooks/disputes/),
[dispute entity](https://razorpay.com/docs/api/disputes/entity/) and
[chargeback guidance](https://razorpay.com/docs/payments/refunds/chargeback/) inform retention and
holds; they do not decide this product's commercial disposition.
[Settlement recon](https://razorpay.com/docs/api/settlements/fetch-recon/) documents typed report
fields and pagination; fee/tax, membership, timing and exhaustive coverage require Q14 evidence.

## Source and migration changes

| Files | Justification |
|---|---|
| `payments/service.py`, `razorpay.py`, `core/config.py` | Order-level finality, authorized hooks, all refund contradictions including legacy account, later provider success, bounded secret overlap and final native replay deadline |
| `payments/accounting.py` | Recurring cases, current cause, common hold reasons, technical failure/retention resolution and commercial liability closure only against verified paid obligation |
| `payments/refunds.py` | GET-first execution, complete refund inventory preflight, held/legacy guards, durable preflight uncertainty and fresh final local checks; provider I/O stays outside DB transactions |
| `payments/reconciliation.py` | Approval retains unpaid cases, live role rechecked, now-mapped unmatched captures receive fresh existing inquiry, token-safe scheduling and bounded aging attention |
| `payments/manager_recovery.py` | Audited fresh evidence investigation, current-proof hold resolution and recovery of the existing original operation; no financial status override/new payout identity |
| `api/routes/manager_finance.py` | Bounded complete child/global traversal, open liability inventory, typed audit response including unmapped investigation, executable recovery endpoints |
| `customer_reads/finance.py`, `projections.py`, `repository.py`, `api/routes/customer_finance.py` | Existing customer CONFIRMING status for unresolved held refund; actual verified SUCCEEDED remains COMPLETED. Correlated hold EXISTS in the existing refund query preserves the original bounded read budget; no frontend or new enum |
| `payments/inventory.py`, `settlement.py`, `workers/financial_inventory.py` | Old creation-time history and report days revisit, visible failed checkpoints, latest immutable assessment including A→B→A, independent report failure handling |
| `reliability/publisher.py`, `workers/financial_reconciliation.py` | Actual identifier-only exception attention route through existing logging, repeated configured aging; monetary outbox routes retained |
| `payments/models.py`, `0022_financial_review_recovery.py` | Add recurrence/attention/checkpoint times and typed audit links; relax audit Payment FK for unmapped evidence with target CHECK. No new table, amount/state/key backfill or generic reference |
| `deploy/runtime-env.names`, `infra/terraform/nonprod/variables.tf` | Explicit previous-secret deadline and optional existing Key Vault secret binding; enabled secret set remains opt-in, no new resource |
| N, R, `test_razorpay_payments.py`, `tests/unit/test_razorpay.py`; schema/ER/idempotency docs | PostgreSQL timing/retained money/provenance proofs, corrected provider-phase fixtures, actionable authorized-event assertions and malformed signed-event rejection; financial/authentication invariant assertions retained |

0022 backfills only case detection time from created_at. Existing Payment, Attempt, Charge,
Refund and their identities remain. Real FK/CHECK and append-only audit protections were tested.
Populated compatible downgrade/upgrade preserves historical command/refund facts. Downgrade
refuses when it would discard new audit provenance or leave an audit without its real target;
use forward recovery in that situation. No destructive cleanup exception was added.

The rejected automatic stored-success replay proposal was discarded. The implemented recovery
uses fresh authenticated provider GETs through the existing reconciliation path, then links
retained history only to already verified matching charges. It reconstructs no monetary
authority from partial stored metadata.

A locally unclaimed PENDING refund with no provider ID and no first-submission timestamp is an
unsubmitted intent: every worker commits that timestamp before provider I/O. Verified correction
of a settlement hold therefore does not require fabricating a provider refund for that intent.
It remains unpaid and visible; the original worker must still pass its fresh GET/hold guards.
A claimed/uncertain original without a provider ID remains blocked: an empty inventory cannot
prove that its remote side effect never happened. The final transaction uses current locked
local facts, so it cannot mistake an already claimed operation for an unsubmitted intent.

## Executable Operations recovery and lifecycle

Every command below requires live MANAGER authorization and a fresh `command_id` UUID; exact
replay returns its immutable audit. A new investigation uses a new command ID. Obtain a token
through the existing identity flow; do not copy raw credentials or provider payloads into logs.

| Work | Read/action | Verified resolution or retained blocker |
|---|---|---|
| Unpaid or held liability | GET `/v1/manager/financial/payments`, `/exceptions`; payment detail section cursors | Approval is visible as authorization, never paid completion. Verified full payout + cleared holds closes relevant debt cases |
| Known uncertain operation | POST `/refunds/{id}/reconcile` with command_id | Known provider GET updates same Refund; timeout/pending never establishes failure/absence |
| Original lost/never-sent/DLQ | POST `/refunds/{id}/recover-original` with command_id | Fresh GET can resolve outcome; confirmed-window replay queues same economic identity. No confirmed window means PROVIDER_BLOCKED. Never reset timestamp/key or issue another refund |
| Failed original/replacement | POST `/charges/{id}/refund-approvals` with command_id and failed_refund_id | Fresh definitive nonpayable proof, exact mapped inventory, obligation/balance/role checks; one replacement under concurrent commands. Later original payable truth reholds, records overpayment if both actually paid |
| Evidenced mechanical hold | POST `/exceptions/{id}/resolve` with command_id and report_day when settlement-related | Fresh correct refund/report plus unchanged cause and complete inventory. Other holds yield VERIFIED_CASE_RESOLVED_HELD. Contradiction, missing coverage or role revocation cannot authorize release |
| Unmapped event/movement | GET `/provider-events`, `/settlement-evidence`; POST respective `/{id}/reconcile` with command_id; report_day for settlement | Verified mapping uses common processor and typed audit. Otherwise blocker/owner disposition retained. GET `/audit` includes standalone audits before Payment mapping |
| Failed/missing scan | GET `/scan-checkpoints` | Repair credential/rate/report issue and rerun `python -m tirodhan.workers.financial_inventory` with explicit settings. Offset is preserved; invalid provider page is not silently skipped; configured history cycles |
| Pending transport | GET `/refund-deliveries`; inspect actual broker/DLQ with approved workload identities | Repair outbox publisher/receiver. Original-recovery endpoint can republish same Refund without forging old inbox completion. Actual DLQ redrive retains original message ID, subject and bytes |
| Aging/attention | Exception queue + `tirodhan.financial_operations` JSON records; existing reconciliation/expiry Job | Configured threshold emits bounded repeated attention. A resolved case cannot age-escalate; recurrence receives new occurrence attention. Assigned human acknowledgement is operational, not inferred from PUBLISHED |

Route prefixes in action rows are `/v1/manager/financial`. Provider I/O occurs between short
transactions. Final commands recheck Payment, role, command and target; no external POST is
made by a manager investigation or hold release. Only the established refund worker executes
an approved persisted intent. Historical booking/planning/fulfilment is never reopened.

The tests demonstrate durable obligation → pending execution/reconciliation → retained case
when unresolved → audited manager investigation → verified outcome/hold release or an explicit
provider/owner blocker. Missing hooks, withheld GETs, unconfirmed failures and manager inaction
keep debt visible. Unknown external payouts stay evidence, with the need for disposition
explicit; they do not become an invented local successful Refund.

PostgreSQL cannot atomically join a Razorpay/Dashboard transaction. Fresh preflight reduces
avoidable risk; a later external payout, deduction or hold can still race an in-flight call.
Original native key, exact amount/body, charge caps, later verified truth, held obligation and
overpaid amount visibility provide recovery controls. They do not prove zero double credit
under an unconfirmed provider finality/visibility contract. Q1/Q2/Q4/Q9/Q13 remain release gates.

## Validation and limitations

Pre-fix demonstrations were actual PostgreSQL/provider runs: first four N scenarios **4 failed**;
refund contradiction/legacy truth **2 failed**; unmatched/current-settlement **2 failed**;
attention/history introduction **2 failed, 2 passed**. A4/A7/A8 lacked the old command/cursor
mechanism and were established by source inspection; no nonexistent pre-fix passing/failing
execution is claimed for them. Post-fix focused and final acceptance results follow below.

Intermediate runs: first eight corrected cases **8 passed in 20.75s**; financial interaction run
**89 passed, 4 failed in 211.75s**; subsequent N+R **39 passed, 1 failed in 76.52s**, then
**43 passed in 74.12s**. Additional closure cases **21 passed in 52.31s**. Expanded runs found
three invalid synthetic payload assumptions (**24 passed, 3 failed in 67.91s**), then two helper
configuration errors (**28 passed, 2 failed in 51.90s**). Those are corrected, not hidden/skipped.
The next legacy-account run had **29 passed, 1 failed in 56.68s** because the negative test
expected a domain conflict instead of the adapter's explicit configuration refusal; its
no-POST/unchanged-money assertions remain. A subsequent **31 passed, 1 failed in 54.99s** exposed
a synthetic foreign-merchant payload missing its configured `account_id`; the corrected payload
then produced **33 passed in 52.83s**. Final acceptance below supersedes these interim runs.
The preliminary full regression was interrupted for the last traversal/retention corrections;
it is not acceptance evidence. No Hypothesis, new skip or dependency was introduced.

R's crash/lost-response transport now fails specifically during POST, preserving its original
two-identical-POST/one-economic-effect assertions. New preflight timeout test separately proves
zero POST and retained debt. The conflicting-provider-ID test now asserts refusal, unchanged
ID/uncertainty and zero remote effects. Legacy FAILED→processed asserts actual verified success
and completion rather than treating obsolete local status as authority. These changes strengthen
the intended financial outcome, not simply accommodate source code.

The first completed full regression produced **1035 passed, 3 failed, 1 skipped, 4 warnings in
822.28s**. The existing bounded customer read test caught two extra hold queries (9 SELECTs,
limit 7). Hold detection is now correlated into the original refund query; its original 7/6
query limits, read-only checks and chronological refund assertions remain unchanged. The other
two tests still expected `payment.authorized` to be ignored. The integration test now verifies
SUBMITTED evidence, a PENDING attempt with due inquiry, no completion and no acceptance; the
raw-byte authentication test uses an unhandled event and additionally rejects a signed but
malformed authorized event. Authentication assertions remain. These failures are recorded;
the complete final rerun below is required for acceptance.

Focused verification after the full-run corrections:

```powershell
$env:TIRODHAN_TEST_DATABASE_URL = '<disposable local PostgreSQL/asyncpg URL>'
$env:TIRODHAN_DATABASE_URL = $env:TIRODHAN_TEST_DATABASE_URL
.venv\Scripts\python.exe -m pytest -q --tb=short -o faulthandler_timeout=60 tests/integration/test_financial_review_closure.py tests/integration/test_customer_financial_cancellation.py::test_historical_refunds_are_ordered_and_financial_reads_are_bounded_and_read_only tests/integration/test_razorpay_payments.py::test_captured_validation_and_existing_payment_gates tests/unit/test_razorpay.py::test_exact_raw_webhook_authentication_precedes_json_and_requires_event_id
```

**42 passed in 92.07s**. The 33 dedicated closure cases are included. Actual PostgreSQL and
synthetic HTTP exercise the unchanged refund ordering/query limits and all seven payment gates.

Final complete regression command:

```powershell
.venv\Scripts\python.exe -m pytest -q -x --tb=short -o faulthandler_timeout=60
```

The next complete run passed **1038 passed, 1 skipped, 4 warnings in 813.94s**. During final
source review, the added unsubmitted/claimed settlement-hold variants reproduced one local
deadlock: **1 failed, 1 passed in 9.69s**. The precise correction is described above. That green
run predates this last correction; the final complete rerun result below is acceptance evidence.
No interrupted run is acceptance evidence.
The first expanded post-fix N run had **34 passed, 1 failed in 61.45s**: its synthetic gateway
returns pending on POST, so completion must wait for later verified GET. The test now asserts
SUBMITTED/no completion first, then changes the synthetic provider outcome, performs real
inquiry, verifies SUCCEEDED/completion and replays execution without another POST. All **35 N
cases passed in 74.67s**; the final full run includes both new settlement-hold variants.

**Final acceptance: 1040 passed, 1 skipped, 4 warnings in 670.98s (11:10)**, obtained
2026-10-10 UTC / 2026-10-11 IST with the command above and both local database variables set.
The complete run collected **1041 cases**; every integration case used the real disposable PostgreSQL
database. The sole existing skip is `tests/unit/test_deployment_job.py:99`, the POSIX signal
exit-status check on Windows. Four existing Firebase `MulticastMessage.tokens` deprecation
warnings remain. No new skip, missing-database exclusion or dependency was introduced.
This result includes all 35 dedicated closure cases and supersedes the interim runs above.

Static validation after the final source/test correction:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy
git diff --check
```

Ruff passed; **298 files already formatted**; mypy passed **151 source files**; diff check passed.

Read-only physical schema audit at **2026-10-10T18:46:22.475356Z** imports all model modules from
`migrations/env.py` and runs raw Alembic `compare_metadata` with `compare_type=True`, before any
diagnostic exclusion. The existing baseline diagnostic was executed without writing a new file:

```powershell
$financialSchemaAuditPath = Join-Path $env:TEMP 'tirodhan_financial_schema_audit.py'
Get-Content -LiteralPath $financialSchemaAuditPath -Raw | .venv\Scripts\python.exe -
```

**Raw differences: exactly three** — extension-owned `spatial_ref_sys` and migration-0007 indexes
`ix_collection_request_pickup_location_gist` and `ix_collection_request_planning_batch_id`.
The second comparison excludes exactly those names and yields **zero differences**. No new
filter, dropped artifact or alteration to Alembic's production environment is introduced;
this does not claim an unfiltered `alembic check` is clean. Physical head is
**0022_financial_review_recovery**, PostgreSQL **17.5 (Debian 17.5-1.pgdg110+1)**, PostGIS
**3.5 USE_GEOS=1 USE_PROJ=1 USE_STATS=1**. All **31 financial FKs** are present without cascading
delete; all five capture/refund/audit/settlement/provider immutability triggers remain.
Additional read-only `pg_constraint`/`information_schema` queries verify nullable audit Payment,
`fk_audit_exception`, `fk_audit_settlement` and `ck_financial_audit_target` on the physical DB.
The N populated round-trip and guarded-downgrade tests verify history preservation and refusal
to discard actual standalone/new-target audit provenance.
The same physical audit was repeated after the complete migration regression at
**2026-10-10T19:18:31.796809Z**: head remained 0022, raw baseline remained exactly three,
filtered differences remained zero, all 31 FKs and all five immutable-history triggers remained.

Terraform validation uses the exact repository-required **1.13.5**, previously checksum-verified
against HashiCorp SHA256SUMS, with the existing AzureRM provider cache:

```powershell
$financialTerraform = Join-Path $env:TEMP 'tirodhan-terraform-1.13.5/terraform.exe'
& $financialTerraform -chdir=infra/terraform/nonprod fmt -check -recursive
& $financialTerraform -chdir=infra/terraform/nonprod validate -no-color
```

Both passed. No Terraform plan/apply or cloud provisioning occurred.

## Costs, remaining enablement and independent review

No paid service or new Azure resource is introduced. Two additional bounded account history
lanes add at most `2 * configured_page_budget` inventory pages per job run, alongside existing
forward/report reads; history depth and cadence determine eventual revisit latency. Refund
preflight now performs repeated bounded inventory GETs before a still-avoidable POST. Native
collection exhaustion can perform up to three passes within its explicit page budget; configure
merchant quotas before enabling throughput. Extra DB observations and identifier-only log
records consume existing resources. No unverified monetary cost estimate or production quota
is supplied.

Production requires O2 deployment/identities/log shipping/responders plus provider/account
evidence in Q1–Q15. Finality stays false and native replay window stays unset by default.
Previous secret overlap is explicitly opt-in and expires. O1/O3 remain owner decisions; current
code retains holds and unpaid amounts instead of choosing a commercial rule.

The local corrections can be independently reviewed with the complete findings register.
This report does **not** claim production readiness or external monetary completion from local
tests alone. Local commit and clean-tree verification will be recorded in the final delivery;
the commit is not pushed or deployed.

## Merge authorization and owner-approved refund replay clarification — 2026-10-11

The owner accepted merge of the Phase 2 financial implementation **with detailed documentation**, while retaining all enumerated live-money/provider/Operations release gates. See the [Phase 2 financial merge and operations runbook](PHASE_2_FINANCIAL_MERGE_RUNBOOK.md) and [updated failed-refund recovery policy](FAILED_REFUND_RECOVERY_POLICY.md).

**Replay eligibility is dynamic, not irreversible:** the system retains `INITIATION_UNCERTAIN` and its actionable financial exception when safe same-key replay is unavailable. It recomputes eligibility against immutable first `processing_started_at` using the *currently configured* window. Increasing that duration can re-enable the original operation after new read-only verification and all other safety gates; it never creates a replacement refund or resets its key. A permanent `UNRESOLVED_EXPIRED` status is **not** part of the approved design. This is not permission to configure an unverified provider-guaranteed window.

The synthetic provider tests exercise original-key recovery and the HTTP-boundary deadline; a dedicated test of expiry followed by later configuration enlargement was not reported executed and remains a regression recommendation. Existing final reported validation stays **1,040 passed, 1 skipped, 4 warnings**, not a fresh post-documentation test run. Razorpay normal-refund retention/finality and real deployed Operations remain explicitly **unverified/not enabled**.

Merging into `main` is source integration, **not deployment**, not Azure provisioning and not live-money authorization. This addendum is a documentation record, not a replacement for the 54-item closure matrix.
