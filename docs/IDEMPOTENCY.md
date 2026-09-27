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

Where a database change must produce an asynchronous event, write the domain change and `outbox_event` in the same transaction.

The publisher may still deliver more than once. Consumers must remain idempotent.

The contract is:

> at-least-once transport + exactly-once intended business effect

not distributed exactly-once execution.

## Operation matrix

| Operation | Idempotency/business key | DB/domain protection | Replay/concurrency behaviour |
|---|---|---|---|
| Request OTP | client command + phone | idempotency record + abuse/rate limits | same command must not unintentionally send another SMS; explicit resend is a new command |
| Verify OTP | verification transaction/command | session creation boundary | retry cannot create multiple sessions |
| Refresh session | **TBD with approved refresh-session strategy** | revocable session + credential verifier/concurrency protection appropriate to that strategy | retry/replay must not create a second unintended session effect; exact lost-success-response vs credential-reuse semantics remain open |
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
| Reassignment | predecessor assignment + command | assignment history + idempotency record | one successor assignment |
| Create pickup execution | request ID | `UNIQUE(request_id)` | exactly one logical pickup execution |
| Create pickup attempt | client attempt ID | unique client attempt | mobile/offline retry does not duplicate history |
| Mark collected | pickup execution + command | conditional state transition | completion happens once |
| Open incident | client incident ID | unique constraint | retry returns same incident |
| Resolve incident | incident + command | conditional transition | one durable resolution |
| Register evidence capture | client capture ID | unique constraint | same capture maps to one record |
| Register media asset | client media ID | unique constraint | same logical file maps to one asset |
| Upload media | media asset/stable object key | object-key uniqueness | retry targets same logical object |
| Finalize media | media asset + expected metadata/hash | conditional transition | replay safe; conflicting metadata rejected |
| Create handover | client handover ID | unique constraint | mobile retry creates one handover |
| Link pickup to handover | `(handover_id, pickup_execution_id)` | composite PK | duplicate link impossible |
| Validate handover | handover + command | conditional state transition | one authoritative outcome |
| Complete request | request terminal transition | conditional transition + validated-handover constraint | completes once |
| Create outbox event | deterministic event key | unique event key | state retry does not duplicate logical event |
| Consume Service Bus message | `(consumer_name, message_id)` | inbox PK | same transport message processed once |
| Send notification | logical notification/business event | unique notification key/record | retry does not create duplicate logical notification |
| External notification call | delivery ID | provider idempotency/reconciliation | uncertain provider outcome is reconciled rather than blindly repeated |

## Explicitly open idempotency decision

### Refresh-session retry after lost successful response

Authentication must not silently adopt a rotation/reuse policy before ADR-007 is resolved.

The final strategy must define what happens when:

1. a refresh request succeeds server-side;
2. the response containing the client-visible continuation credential is lost;
3. the client retries using the previously presented credential.

The implementation must distinguish, or deliberately choose not to distinguish, this ambiguous network-retry case from hostile credential reuse. That security/UX trade-off is an architecture decision.

## Critical concurrency scenarios

### Cancellation vs planning freeze

Both attempt a conditional transition from `ACCEPTED`. Only one may win. No read-then-write race in application code.

### Two riders accept same group

The database guarantees at most one active assignment. One rider wins; the losing request receives an already-assigned result.

Phase 1H serializes assignment by locking the collection group, then rider profile, rider
availability, offer when present, and finally group pickups in identifier order. Offer creation
replays on `(collection_group_id, rider_id, offer_round)` only when the requested expiry matches.
Accepted-offer and same-rider manual retries return the established active assignment. The group
partial unique index and unreleased-pickup partial unique index remain physical backstops.

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
