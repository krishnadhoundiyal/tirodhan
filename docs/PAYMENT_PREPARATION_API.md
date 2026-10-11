# Payment preparation and cancellation/replacement contract

Accepted owner scope: P1-1 implementation, 2026-10-11. Backend only; frontend integration,
independent code review, PR/merge and deployment are separate steps.

## Customer commands and identities

Freeze declared items, quotations and payment money when the first durable attempt exists.
Changes after initiation require explicit cancellation and new booking creation. There is no
item-edit endpoint, QuoteRevision or cross-booking credit. Existing creation accepts a new stable
clientRequestId, a fresh serviceabilityContextId, changed items and current eligible slot. The
server quotes again. Context uniqueness and command fingerprinting protect creation/replay.

Persist the cancellation command key and new clientRequestId before submitting. A lost response
requires exact replay, then status refetch. Never create B because cancellation merely timed out;
confirm committed cancellation first. Different replacement IDs represent different bookings.
The old slot can become unavailable after cancellation; B is a fresh eligibility decision.

After A cancellation commits, independently eligible B may be paid while A remains unresolved.
No account-wide unresolved-payment scan blocks B. Explain before B payment that A's remote Order
remains active, temporary double debit is possible, and eligible verified A captures receive
refund processing. Do not promise fixed bank-credit timing. Refusal at cutoff/freeze is a refusal
to cancel; the UI must not announce cancellation or offer a replacement as already authorized.

## POST /v1/payments/collection-requests/{request_id}/attempts

Authenticated customer; required Idempotency-Key. Existing HTTP 201 contract remains. This is
now bounded preparation/resumption, not a request to allocate a new Order for every client key.

Response retains payment_attempt_id, status, provider, provider_order_id, provider_payment_id
and controlled failure_code; adds typed action, action_reason and nullable checkout_ready_until.
No secret, merchant-account fingerprint, raw provider body, instrument address, phone or token
is returned. Order ID and public Checkout key are required protocol data, not private credentials.

| action | Meaning | Client behavior |
|---|---|---|
| START | No exposure yet, or original Order verified for first Checkout | Prepare if needed, then obtain fresh Checkout GET; only that GET supplies handoff parameters |
| RESUME | Failed instruments; original Order verified eligible | Obtain Checkout GET for the same attempt/Order; never create another booking to retry unchanged items |
| WAIT | Preparation busy, authorized/processing instrument, missing/incomplete inquiry or reconciliation | Show verification pending; refetch status within the bounded UX policy |
| UNAVAILABLE | Booking/payment ineligible, wrong/unverified account, already received money, or historical command superseded | No Checkout; display booking/payment/refund states separately |

Reasons are NEW_PAYMENT, CHECKOUT_READY, VERIFICATION_REQUIRED, PREPARATION_IN_PROGRESS,
RECONCILIATION_REQUIRED, BOOKING_INELIGIBLE, ACCOUNT_MISMATCH, PAYMENT_RECEIVED and
SUPERSEDED_ATTEMPT. They are controlled values; clients must handle unknown future reasons
conservatively. START on the status GET does not itself contain an Order: call preparation first.

Exact commands replay their recorded attempt identity, with current readiness/eligibility.
Fresh keys converge on the original exposed Order. An exact historical command for a positively
rejected first creation remains that failed resource; a fresh key can create a new attempt only
under that narrow rejection proof. Once another exposed attempt exists, the historical command
returns UNAVAILABLE/SUPERSEDED_ATTEMPT; use the current payment projection/preparation command.
No rejection rule relies on an empty GET, short timeout, failed instrument or age.

A status GET cannot grant new readiness: it only projects persisted evidence. When the customer
asks to retry or verify again, call preparation with the retained command key even if the read
action is WAIT. WAIT blocks native Checkout, not another bounded preparation inquiry. Serialize
these requests, respect the configured verification window/backoff, and obtain fresh Checkout GET
only after preparation returns START/RESUME. Status-only polling does not reopen Checkout.

Concurrent preparation returns the shared attempt with WAIT while the owner holds the existing
PostgreSQL lease. It need not synchronously return the provider Order before the first request
finishes. Cached unexpired readiness avoids redundant exact/fresh-key GETs. A new mapped provider
observation invalidates readiness and increments a fencing counter; it does not permit a new
Order. Multiple retained exposed Orders fail closed for customer preparation and remain eligible
for financial discovery.

No new attempt exists for an ineligible first preparation: HTTP 409. Existing terminal attempt
commands return 201 with UNAVAILABLE and no remote creation. Conflicting fingerprints/in-progress
idempotency claims remain HTTP 409. Missing planning, retention or provider configuration is 503;
merchant mismatch on an existing attempt is typed UNAVAILABLE. Neither creates another Order.

## Provider readiness and first-POST recovery

The server authenticates the persisted merchant binding, original receipt/Order and exact frozen
money. It reads Order status, paid/due amounts, attempt count and its instrument collection.
A created Order with zero instruments and zero paid amount can START. An attempted Order with
only verified failed instruments, complete matching counts, no paid amount and full amount due
can RESUME. Any authorized/created instrument, capture, unknown state, contradictory amount,
duplicate instrument ID, malformed or saturated collection grants no Checkout. Captures go to
the existing financial processor. Readiness uses documented Order/instrument fields; our Orders
are created with partial_payment=false, and an explicitly contradictory response option blocks
readiness. Order GET need not echo this creation-only option.

Each HTTP stage has a total deadline of three configured Razorpay HTTP timeout durations; the
combined customer external-I/O budget is at most six such durations. Receipt lookup has an
existing finite page budget. Observation processing is bounded below 100 instrument records.
No DB session/transaction stays open during provider I/O. Permission starts at the verified
snapshot time, not after potentially delayed DB processing. Configure
TIRODHAN_PAYMENT_CHECKOUT_READINESS_SECONDS explicitly within 1..60; unset fails preparation
closed. This is a technical handoff lifetime, unrelated to payment/refund finality.

New durable local intents set order_creation_pending=true. After fresh Payment-first booking,
planning, quote and account checks, the single permitted first POST commits this flag false.
A crash before that point can resume the unsent intent. A crash after authorization, ambiguous
POST or delayed lookup can only GET the original receipt; absence cannot authorize a second
POST. Legacy rows default false, because their first-POST history cannot be invented. If an
unsent legacy intent cannot be distinguished from exposure, keep WAIT and involve Operations.
An already-started POST may finish after cancellation; retain its real Order and schedule inquiry,
without enabling Checkout. Cancellation during the pre-POST lookup revokes create permission.

## GET /v1/customer/payment-attempts/{attempt_id}/checkout

Authenticated ownership; remains read-only and private, no-store. No provider HTTP or financial
mutation occurs. Existing public fields remain; checkout_ready_until is added alongside the
booking expires_at. Require current permission, exact account/key ownership, PENDING attempt/
Payment/booking, matching frozen money, unexpired booking and pre-cutoff/unfrozen work unit.
A final joined eligibility query rechecks all handoff predicates in one committed-state snapshot,
including blocking provider evidence. A cancellation/capture/authorization committed during the
earlier reads revokes handoff. Expired permission or unsafe state gives 409; wrong ownership gives
404; incomplete runtime/money configuration fails closed.

This is still a snapshot. Neither DB rechecks nor provider GET guarantee that the remote state
cannot change after response. Multiple devices can retain a previously issued same-Order Checkout.
The backend cannot revoke that external screen or cancel the remote Order. Reconciliation and
charge-specific accounting remain necessary. Public Order constraints are documented by
[Razorpay Orders](https://razorpay.com/docs/payments/orders/); fetch fields and instrument collection
are documented by [Order GET](https://razorpay.com/docs/api/orders/fetch-with-id/) and
[Order payments](https://razorpay.com/docs/api/orders/fetch-payments/).

## Status projections, countdown and refunds

Payment GET and embedded collection detail add action, action_reason, verification_pending and
nullable checkout_ready_until. retry_allowed is true only for START/RESUME under current booking/
merchant/reconciliation checks. An established old FAILED Order projects CONFIRMING, not terminal
customer failure. PENDING Orders whose permission expired project CONFIRMING until preparation
verifies readiness. The actual provider/domain state is not changed by this projection.

Cancelled booking status stays CANCELLED. Its payment can remain CANCELLED with
verification_pending=true, then become financially SUCCEEDED after verified late capture while
the collection remains cancelled. Refund states remain INITIATED, PROCESSING, CONFIRMING, FAILED
or COMPLETED; only verified payout maps to COMPLETED. No ordinary compensation approval screen
is required. Financial holds and uncertain/failed refund recovery retain existing safeguards.

The eventual mobile implementation should refetch on callback, focus, resume and push, with one
in-flight refresh and a configurable bounded, jittered foreground verification window. A roughly
three-minute countdown is illustrative. On its expiry stop automatic polling, retain actionable
verification-pending text and offer manual refresh/details and eligible cancellation. Do not run
continuous background polling or hold a FastAPI request open for the countdown. No callback,
timer, dismissal or navigation may mark financial failure/success or allocate another Order.

## Historical recovery and release ownership

The existing reconciliation job selects bounded, leased known FAILED Orders with null due time.
Persisted Order identity is evidence warranting a GET, not a blind status rewrite. Fresh failed
instrument evidence moves the attempt to nonterminal PENDING; verified captures enter normal
accounting/compensation. Unknown/unavailable inquiry retains financial truth and reschedules.
Missing/mismatched account ownership skips external GET, reschedules and raises configured aged
Operations attention. Operations must verify merchant history through existing audited recovery;
never guess the active account or bulk rewrite FAILED rows.

Deploy the existing reconciliation/inventory/refund/outbox/webhook jobs and monitor their configured
rate, visibility coverage, backlog/DLQ and named Operations responder. Merchant key rotation requires
the same verified account ID; without it a key-derived fingerprint change deliberately blocks
handoff. Public [receipt reuse documentation](https://razorpay.com/docs/api/orders/create/) does not
establish linearizable visibility, notification deadlines, refund replay retention or definitive
refund non-payability. Existing refund gates remain unset/false until merchant-specific approval.
Local PostgreSQL/mock HTTP tests do not establish live merchant or device behavior.

See [implementation matrix and validation](PAYMENT_P1_1_IMPLEMENTATION_REPORT.md).
