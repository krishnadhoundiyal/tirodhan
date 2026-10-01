# Idempotency

## Architectural rule

Idempotency is a system-wide invariant.

> Every state-changing operation that can be retried, replayed, duplicated, redelivered, timed out, or invoked concurrently must have an explicit idempotency strategy.

A feature is not complete until its duplicate, retry and concurrency behaviour is documented and tested.

Idempotency does not mean every replay returns success. Security-sensitive credential replay may correctly be rejected according to the approved authentication policy. The invariant is that replay cannot create an unintended second business effect.

## Four defensive layers

```text
Client/API command idempotency
        ↓
Database/domain business constraints
        ↓
Message inbox + transactional outbox
        ↓
External-provider idempotency / reconciliation
```

No single layer replaces the others.

Service Bus duplicate detection does not replace DB constraints.

HTTP idempotency keys do not replace domain uniqueness.

Inbox deduplication does not replace business-level idempotency.

## Generic API command pattern

`idempotency_record` is used for retriable client/API commands where appropriate.

Key:

```text
(scope, idempotency_key)
```

Behaviour:

```text
same key + same request fingerprint
→ return/reconstruct previous result

same key + different request fingerprint
→ reject
```

## Transport pattern

Service Bus is treated as at-least-once delivery.

```text
receive message
    ↓
begin DB transaction
    ↓
claim/check inbox_message
    ↓
already processed?
    ├── yes → no business effects
    └── no  → execute domain operation
    ↓
mark inbox processed
    ↓
commit
    ↓
settle message
```

If the process crashes after DB commit but before settlement, redelivery is harmless.

## Outbox pattern

### Phase 1T serviceability runtime

Only explicitly registered outbox types are published (initially ServiceabilityRequested).
Each finite publisher Job selects a bounded due batch, increments publish-attempt metadata
in a short transaction, sends outside PostgreSQL, then marks PUBLISHED on success. A crash
after send may resend the same outbox UUID as transport message ID. Unrelated events remain
pending. Concurrent Jobs may duplicate transport, never intended business effects.

Consumer identity is `serviceability-resolver`; transport key is `(consumer_name, message_id)`
and business key is the serviceability-context UUID. It commits PROCESSING first, invokes
the shared resolver outside that transaction, then commits PROCESSED before Peek-Lock
settlement. PROCESSING redelivery resumes; PROCESSED skips work. Crash after terminal commit
but before inbox completion/settlement replays the terminal result without Google. Broker
delivery count is never a business attempt number. Invalid envelopes are rejected without
copying raw message content into inbox/logs. Infrastructure/configuration failures remain
unsettled/recoverable; terminal technical domain outcomes can be durably acknowledged.

Both worker and checkout read immutable input in a short session, call Google outside DB,
then conditionally update PENDING -> terminal. The loser returns the winner's persisted
result. Checkout completed replay precedes all provider/pricing work; fresh checkout validates
ownership/expiry before resolving PENDING, prices only after serviceability, and revalidates
the context in its final request transaction. No distributed coordination is needed.

Where a database change must produce an asynchronous event, write the domain change and `outbox_event` in the same transaction.

The publisher may still deliver more than once. Consumers must remain idempotent.

The contract is:

> at-least-once transport + exactly-once intended business effect

not distributed exactly-once execution.

## Operation matrix

| Operation | Idempotency/business key | DB/domain protection | Replay/concurrency behaviour |
|---|---|---|---|
| Request OTP | scope `auth.start` + `client_request_id`; fingerprint of client UUID + phone HMAC | committed idempotency reservation + AuthenticationChallenge uniqueness + phone advisory lock + active-challenge partial uniqueness | only claim creator invokes Generate; IN_PROGRESS conflicts without provider call; COMPLETED replays its live ACTIVE challenge; changed phone, terminal or expired replay conflicts; new ID creates a new intent |
| Verify OTP | scope `auth.verify` + `client_login_id` | durable challenge/HMAC fingerprint + challenge row lock + phone advisory lock + active-phone uniqueness | exact completed replay does not call provider or mint credentials; one challenge produces at most one committed login; changed fingerprint conflicts |
| Refresh session | SHA-256 refresh-credential verifier | refresh-session row lock + fixed expiry/revocation | stable active credential may refresh repeatedly/concurrently; no rotation, successor, or expiry extension |
| Logout session | SHA-256 refresh-credential verifier | refresh-session row lock + conditional `revoked_at` | active session is revoked; revoked or unknown credential is generic success |
| Grant role | `(user_id, role_code)` | unique active role membership | repeat grant returns existing membership/no-op |
| Revoke role | role membership | conditional transition | repeated revoke remains revoked |
| Add address | user + client command | idempotency record | retry returns same address |
| Edit address | address + expected version | optimistic concurrency | same replay safe; stale conflicting edit rejected |
| Archive address | address | conditional transition | replay remains archived |
| Create serviceability context | user + client command | idempotency record | same context returned |
| Resolve serviceability | context + input snapshot/version | conditional terminal update | worker/API race produces one authoritative result |
| Create collection request | `(customer_id, client_request_id)` | unique constraint + request fingerprint | retry returns same booking |
| Initiate payment attempt | payment-attempt ID | unique attempt + provider idempotency | retry does not create another provider order |
| Payment webhook | `(provider, external_event_id)` | unique provider event | replay acknowledged without duplicate business effect |
| Confirm payment | payment ID | conditional state transition | request accepted once |
| Late success from another attempt | provider payment ID | unique external reference + reconciliation | record truth; do not accept request again; reconcile/refund duplicate charge |
| Cancel request | request + cancellation command | atomic `ACCEPTED -> CANCELLED` | same cancellation replay returns established result |
| Create refund | logical refund ID | unique refund operation + payment-row serialization | one logical refund |
| Call refund provider | refund ID | provider idempotency where supported | retries cannot create multiple provider refunds |
| Refund webhook | provider event ID | unique provider event | apply once |
| Planning scheduler | `(cell_id, slot)` | unique planning batch | overlapping runs produce one batch |
| Freeze requests | planning batch | conditional request transitions | request cannot be frozen into multiple batches |
| Start logical compaction attempt | `(batch_id, attempt_number)` | unique constraint | broker redelivery is not automatically a new business attempt |
| Commit planning result | planning batch | completed-state check + unique membership | redelivery after completion has zero business effects |
| Singleton fallback | planning batch | same planning-result boundary | fallback persisted once |
| Generate rider offer | `(group, rider, offer_round)` | unique constraint | retry does not duplicate same-round offer |
| Rider accepts offer | group/offer command | one active assignment constraint | same rider retry gets existing result; concurrent riders yield one winner |
| Fleet auto-assignment | group/generation | assignment constraints | retry cannot double assign |
| Manual assignment | manager command + group | assignment constraints | double-click cannot double assign |
| Reassignment | `pickup-reassignment` + `client_reassignment_id` | assignment history + idempotency record + active-group/current-item partial uniqueness | exact replay returns the recorded successor in any later assignment state; a different command cannot create another successor |
| Create pickup execution | request ID | `UNIQUE(request_id)` | exactly one logical pickup execution |
| Record pickup attempt | `(pickup_execution_id, client_attempt_id)` | unique client attempt and per-pickup attempt number | exact replay returns history; conflicting outcome is rejected; successful attempt and collection are atomic |
| Open incident | client incident ID | unique constraint | retry returns same incident |
| Resolve incident | incident + command | conditional transition | one durable resolution |
| Register evidence capture | scope `evidence.capture` + client capture ID | idempotency record + unique client capture ID + unique capture link | same actor/target kind/target ID/UTC capture time replays the capture before current target validation; a changed field conflicts |
| Register media asset | scope `media.register` + client media ID | idempotency record + unique client ID/evidence capture/object key | same evidence/actor/media type/expected content type replays the asset before current evidence validation; a changed field conflicts |
| Authorize media upload | media asset ID | persisted stable object key + pending-state check | retry may issue a fresh ephemeral authorization for the same key; no token is persisted |
| Finalize media | media asset ID | row lock + conditional `PENDING_UPLOAD -> FINALIZED` | storage is inspected without a held DB lock; terminal replay returns the asset without another inspection |
| Create handover | scope `handover.record` + client handover ID | idempotency record + unique client ID + partial unique validated pickup | same fingerprint replays the event; changed rider/point/sorted pickups/observed coordinates conflicts; sorted pickup locks allow one validated winner while rejected history remains retryable under a new client ID |
| Link pickup to handover | `(handover_id, pickup_execution_id)` | composite PK | duplicate link impossible |
| Validate handover | handover + command | conditional state transition | one authoritative outcome |
| Complete request | request ID | request-row lock + natural `PLANNED -> COMPLETED` terminal transition | completed replay returns the established timestamp without re-evaluating prerequisites |
| Create outbox event | deterministic event key | unique event key | state retry does not duplicate logical event |
| Consume Service Bus message | `(consumer_name, message_id)` | inbox PK | same transport message processed once |
| Send notification | logical notification/business event | unique notification key/record | retry does not create duplicate logical notification |
| External notification call | delivery ID | provider idempotency/reconciliation | uncertain provider outcome is reconciled rather than blindly repeated |

## Critical concurrency scenarios

### Identity creation and session commands

Phase 1R start claims `(auth.start, client_request_id)` in a short transaction with a fingerprint
of client UUID plus phone HMAC, never plaintext/encrypted phone. That reservation commits before
Generate. Only its creator may call the provider; simultaneous same-key requests cannot create a
second provider transaction. IN_PROGRESS returns controlled 409; COMPLETED loads result_resource_id
as the challenge and replays only while ACTIVE and unexpired. Existing pre-reservation challenges
are attached to a completed claim without another provider invocation. Different client request IDs
remain separate intents. Generate holds no DB transaction. Its success is followed by a transaction
that locks the prior ACTIVE phone challenge, supersedes it, inserts the new challenge and completes
auth.start with challenge ID and status 202. Provider failure leaves the old intent unchanged.
An ambiguous Generate timeout/network failure or post-invocation DB failure retains IN_PROGRESS;
neither elapsed expires_at metadata nor replay permits automatic takeover or provider retry. Use a
new client request ID. Known local failures before provider invocation roll back the claim normally.
The provider/DB side-effect gap remains; no recovery worker or distributed transaction is introduced.
Terminal/expired challenge request IDs cannot send again.
Verify fingerprints only challenge identity and phone HMAC. Completed login replay precedes
provider invocation. After Validate success it locks and revalidates the challenge, claims the
command, resolves identity under the phone advisory lock, creates the session, consumes the
challenge and completes idempotency in one transaction. Concurrent verification or supersession
can yield only one committed login per challenge. Separate challenges can create independent
sessions for one user. Provider success with local rollback leaves no committed login; an
already-verified provider response is an authentication failure requiring a new start.

Refresh and logout serialize on the same refresh-session row. Two refreshes may both issue access
JWTs because the stable credential and fixed expiry do not change. If logout wins, later refresh
fails; if refresh wins, logout subsequently revokes the session and live authorization rejects that
JWT on its next use. Completed initial-login replay never creates replacement credentials because
no recoverable bearer credential is persisted.

### Cancellation vs planning freeze

Both attempt a conditional transition from `ACCEPTED`. Only one may win. No read-then-write race in application code.

### Two riders accept same group

The database guarantees at most one active assignment. One rider wins; the losing request receives an already-assigned result.

Phase 1H serializes assignment by locking the collection group, then rider profile, rider
availability, offer when present, and finally group pickups in identifier order. Offer creation
replays on `(collection_group_id, rider_id, offer_round)` only when the requested expiry matches.
Accepted-offer and same-rider manual retries return the established active assignment. The group
partial unique index and unreleased-pickup partial unique index remain physical backstops.
From Phase 1I, terminal offers retain `resolved_assignment_id`, so accepted-offer replay and the
same-rider manager-won convergence remain valid after that assignment becomes `COMPLETED`.
Creation of a genuinely new initial offer additionally requires a nonempty group whose entire
pickup population remains `PENDING_ASSIGNMENT`; existing offer-key replay is checked first.

### Concurrent pickup attempts

Phase 1I locks the rider assignment before rider availability, pickup execution, and current
assignment item. This serializes attempt-number allocation and final-pickup completion for all
work owned by an assignment. Exact `(pickup_execution_id, client_attempt_id)` replay is checked
immediately after locking the historical assignment, so it remains valid after pickup and
assignment completion. Different attempts cannot mutate an already-collected pickup.

### Concurrent handover recording

The handover command claims `(handover.record, client_handover_id)` before domain validation. Its
fingerprint contains rider, receiving point, canonically sorted pickup IDs, and explicit observed
latitude/longitude. Exact completed replay returns the recorded event before consulting current
master or assignment state. A fresh command locks the receiving point and then pickup executions
in UUID order. Those pickup locks serialize both competing valid handovers and collection versus
handover; the partial unique validated-item index is the final database backstop. Rejected events
remain durable but do not prevent a later command from validating the pickup.

### Concurrent evidence capture registration

The evidence-capture command claims `(evidence.capture, client_capture_id)` before fresh target
validation. Its fingerprint contains the actor, target kind, target ID, and capture time normalized
to UTC. Exact completed replay returns the recorded capture and link before consulting current pickup,
handover, assignment, rider, or receiving-point state. Concurrent exact submissions converge on the
same capture through command idempotency and `client_capture_id` uniqueness. Fresh registration uses
ordinary reads because it records a new immutable fact without reserving or transitioning the target;
the typed link and its unique capture constraint are the database backstop for exactly one target.

### Concurrent media registration and finalization

Registration claims `(media.register, client_media_id)` with a fingerprint of evidence capture,
requesting user, media type, and expected content type. Exact concurrent commands converge on one
asset and its server-generated object key; `UNIQUE(evidence_capture_id)` enforces one original file
per capture. Finalization uses natural row idempotency rather than `idempotency_record`: callers may
inspect storage concurrently without holding PostgreSQL locks, then serialize on the media row.
The first pending caller records the terminal metadata/timestamp; later callers return that exact
terminal row without overwriting it or reinspecting once they observe it finalized.

### Concurrent collection-request completion

Completion uses the `CollectionRequest` row itself as the natural idempotency and serialization
boundary; it creates no `idempotency_record`. The transaction begins with a request-row
`SELECT ... FOR UPDATE`, with no unlocked request pre-read. One caller performs the
`PLANNED -> COMPLETED` transition after Option B evidence checks; concurrent or later callers see
`COMPLETED` and return the established row and original `completed_at` without querying pickup,
handover, evidence, incident, assignment, or media state. Prerequisite rows are read but not locked.
No completion outbox event is emitted in Phase 1N.

### Pickup incident and reassignment

Incident creation replays first on globally unique `client_incident_id`, validating the pickup,
historical assignment/rider, and reason before fresh-state checks. This allows a committed incident
to replay after ownership changes. Reassignment fingerprints predecessor, replacement, manager,
and optional incident under scope `pickup-reassignment`, stores the successor assignment as the
result resource, and completes that record in the same transaction as all ownership/rider changes.
The collection group and predecessor assignment serialize group ownership; both rider resources
are locked in stable rider-ID order; pickup executions and predecessor items are locked in pickup-ID
order. Competing fresh commands yield one successor, while exact concurrent commands converge on
the same result. Reassignment has no external side effect and writes no outbox event in Phase 1J.

### Concurrent refunds

Refund creation serializes through the payment row. Total committed refunds must never exceed the original successful payment amount.

### Duplicate planning work

A completed `planning_batch_id` is a terminal idempotency boundary. Redelivery must not create new groups, membership, pickup executions, or downstream events.

Planning control messages carry `planning_batch_id`, `attempt_number`, `message_id`, and a controlled message type. `PlanningBatchReady` is attempt 1; `PlanningAttemptRequested` is an explicit retry N>1. The planning inbox business key is `{planning_batch_id}:{attempt_number}`. A `PROCESSING` inbox row resumes the same `STARTED` attempt; `PROCESSED` has no further business effect.

Successful result persistence, request transitions, attempt/batch completion, the single `PlanningBatchCompleted` outbox event, and inbox completion share one transaction. A controlled planner technical failure consumes the current logical attempt; an infrastructure failure rolls back and consumes none. When the snapshotted maximum is exhausted, the same transaction completes the batch with fallback singleton groups.

### Worker crash after external provider call

If the provider supports idempotency, use a stable provider key derived from the logical local operation.

If the provider does not support idempotency, persist durable local operation state and reconcile before repeating a potentially destructive action.

## Testing standard

Every mutating use case must test:

1. normal success;
2. exact replay;
3. duplicate transport delivery where relevant;
4. concurrent execution where relevant;
5. crash/retry around DB transaction boundaries;
6. crash/retry around external side effects where relevant.

Examples:

```text
same refund command x2
→ one logical refund

same Service Bus message x2
→ one business effect

two riders accept concurrently
→ one winner

worker crashes after DB commit but before message settlement
→ no duplicate effect after redelivery

worker crashes after provider call but before local acknowledgement
→ provider idempotency/reconciliation prevents duplicate charge/refund
```

## Agent rule

No new mutating API endpoint, webhook, worker, scheduled job, external side-effect integration, or mobile state-changing command is complete until its:

- idempotency key/business key;
- DB protection;
- replay result;
- external-side-effect behaviour;
- concurrency behaviour;
- duplicate/concurrency tests

are explicitly defined.
