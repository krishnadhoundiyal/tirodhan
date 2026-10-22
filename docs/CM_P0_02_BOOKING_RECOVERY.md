# CM-P0-02: booking recovery and Customer Mobile handoff

Implemented 22 October 2026 on `codex/cm-p0-02-booking-recovery` from clean `main`
`c1e2e7c7fba12bda3b3df03710418c2f1e7b0d0b` (merged PR #68). Projected holds are
already present in that base (`aef7ae0`). The reference frontend increment is
`5cbff539be35b99253da08daee31e899e0ce04d6`, branch
`codex/customer-projected-slots`; this task does not modify that repository.

## Additive API correction

`GET /v2/customer/slot-holds/{reservation_id}` now returns:

| Field | Meaning |
| --- | --- |
| `reservation_id: UUID` | Original hold's durable identity, equal to `client_command_id` |
| `admission_basis: string` | Existing basis, including `PROJECTED_ALLOWANCE` |
| `bound_request_id: UUID \| null` | Persisted `CapacityReservation.request_id`; null when no binding is committed in this read's snapshot |
| `bookable: boolean` | Existing permission calculation: unbound and server time at or before `book_by` |
| `expires_at: datetime` | Existing reservation expiry, timezone-aware |
| `book_by: datetime` | Existing `expires_at - pending_payment_lifetime_seconds` |

The same shape is returned by `POST /v2/customer/slot-holds`, including exact hold
replay. UUIDs serialize as JSON strings; absent bindings serialize as explicit JSON null.
All previous fields and their calculations are preserved. No new endpoint, schema,
migration, service, infrastructure, numerical physical-load input or production flag is added.

The endpoint checks the live authenticated CUSTOMER role/session, then reservation
ownership before returning data. Missing and foreign reservations both return
`404 SLOT_HOLD_UNAVAILABLE`. It exposes no coordinates, address snapshot, manifest,
price, Payment or provider information. Booking detail has its own ownership check.
Routing APIs still require their existing explicit registration flags and payment
lifetime configuration; this increment does not enable them or change failure behavior
for missing configuration.

Expiry is a permission deadline, not deletion of binding history. There is no expiry,
current projection, serviceability or booking-status filter on the binding read. The
ID survives `book_by`, reservation expiry and later cancellation/payment/completion.
Any bound reservation stays `bookable=false`, including cancelled/expired bookings.
A binding proves a booking committed; it proves neither successful payment nor acceptance.

## Persist before sending

Customer Mobile should durably store, scoped to the signed-in owner:

1. The hold's `client_command_id` and exact command body before hold POST.
2. The booking's `client_request_id` and exact booking body, including
   `capacity_reservation_id`, before booking POST.
3. The returned booking ID when received or recovered.

Use app-private storage for any sensitive pending command material and the platform's
secure credential storage for authentication. Do not attach another account's saved
workflow to a new login. A resumed session or a fresh authenticated session for the
same owner can recover from PostgreSQL without the original process or HTTP response.

## Restart reconciliation contract

Start with `GET /v2/customer/slot-holds/{original client_command_id}`.

| Durable observation | Customer Mobile action |
| --- | --- |
| Hold exists, null binding, no booking submitted, `bookable=true` | Continue that intent using the saved booking command when the physical-load policy is approved and implemented |
| Non-null binding | Save that ID, stop creating bookings, and GET `/v1/customer/collection-requests/{bound_request_id}` |
| Booking POST timed out, null binding | Submission is still ambiguous: the other transaction may be uncommitted. Retry the **same** `client_request_id` and unchanged body, or refresh status. Never generate another booking identity to escape uncertainty |
| Null binding, `bookable=false` | No binding is visible now and this hold cannot authorize a fresh booking. If an earlier POST was uncertain, first exact-replay that command and refresh status to resolve an in-flight commit. Once resolved as uncommitted, acquire a fresh valid context/hold as required and start a new intent |
| Hold POST itself timed out and lookup is 404 | Exact-replay the original hold UUID/body. A completed hold replay returns its original result without extending expiry; a lookup can precede another transaction's commit. Do not interpret a single 404 as a cancellation or payment result |
| 401 / 403 | Restore the correct owner's authentication/authorization before recovery; do not switch the pending intent's owner |
| Network / 5xx / conflict while reconciling | Keep the saved IDs and body, refresh/retry with those same identities; do not assert failure or start payment on a guessed request |

After a binding is recovered, booking detail governs current behavior:

| Detail result | Meaning |
| --- | --- |
| `PENDING_PAYMENT` | Booking exists; use existing payment preparation/handoff eligibility. Binding grants no payment success |
| `ACCEPTED` / subsequent active fulfilment state | Existing financial/domain authority accepted the booking; display the current detail projection |
| `CANCELLED` / `EXPIRED` | Preserve that booking and financial history. Do not reopen its hold or infer refund completion |
| `COMPLETED` | Display authoritative completion; do not create replacement work |
| Financial state changed, including late paid cancellation | Use the detail's independent payment/refund projections. Captured money does not revive a cancelled/expired request |
| Detail temporarily unavailable | Retain the recovered ID and retry the owner-protected detail. No new booking is justified |

`bookable` retains its existing narrow meaning. Fresh booking submission must still
pass all other domain checks, including slot cutoff, context, declaration and expiry.
A read is a snapshot, not a lock or a promise that another in-flight transaction failed.

## Existing mutation and replay boundary reviewed

| Concern | Existing authority, unchanged |
| --- | --- |
| Booking business key | `(customer_id, client_request_id)` with exact command fingerprint, including reservation, context, slot and item declarations |
| Database protection | Unique customer/client key, unique serviceability context, reservation row lock, bind-once/deferred ownership/location/window guards, one manifest per request and one logical Payment |
| Transaction | Command claim, request/items/Payment, reservation binding, RoutingManifest, existing wake/source changes and completed command result commit together |
| Replay | Completed command lookup precedes fresh slot/time/context/category/pricing/hold validation; reconstructs the original resource with its current status and historical financial terms |
| Different body | Same key with changed body conflicts; no new booking or rewritten terms |
| Concurrency | Exact commands converge; competing intents cannot consume one context/hold twice. PostgreSQL locking and uniqueness choose the winner; rollback removes the loser's writes |
| External effects | No road/solver/payment-provider call at booking bind. Financial success, provider reconciliation, inbox/outbox, cancellation and custody use their existing boundaries |

No additional backend lookup is needed when the client persists its original hold
identity. If all local workflow identities are lost, the existing bounded owner-scoped
active/history collection lists remain available; this increment adds no heuristic match,
general reconciliation service or new lookup by client key. Retention of business bindings
and replay records must continue to follow approved retention policy; their expiry metadata
alone does not remove a row in the current implementation.

## Files and evidence

- Source: `src/tirodhan/api/routes/routing_orchestration.py`.
- Integration coverage: `tests/integration/test_booking_recovery.py`.
- Proposed, unimplemented physical policy: [PHYSICAL_LOAD_POLICY_PROPOSAL.md](PHYSICAL_LOAD_POLICY_PROPOSAL.md).
- Executed validation and limitations: [CM_P0_02_VALIDATION.md](CM_P0_02_VALIDATION.md).

Payment-first acceptance, projected-slot generation/freshness, operational routing and
evidenced custody are unchanged. Customer Mobile remains blocked from live hold submission
until an approved physical-load source and its implementation are available.
