# ADR-014: System-Wide Idempotency and Reliable Side Effects

**Status:** Accepted

## Context

Tirodhan contains many retryable and concurrent workflows:

- mobile commands over unreliable networks;
- payment/refund provider callbacks;
- Service Bus at-least-once delivery;
- scheduled planning jobs;
- rider offer acceptance races;
- asynchronous media/evidence finalization;
- external notification/provider calls.

Treating idempotency as an implementation detail of individual workers would leave gaps and produce duplicate financial or fulfilment effects.

## Decision

Idempotency is a system-wide architectural invariant.

Every state-changing operation that can be retried, replayed, duplicated, redelivered, timed out, or executed concurrently must explicitly define:

- logical/business idempotency key;
- DB/domain protection;
- transaction boundary;
- replay result;
- concurrency rule;
- external side-effect behaviour.

Use complementary defensive layers:

1. client/API command idempotency;
2. PostgreSQL/domain uniqueness and transactional concurrency protection;
3. durable message inbox plus transactional outbox;
4. provider-native idempotency where supported, otherwise durable reconciliation.

No layer replaces another.

### Command replay

For a command idempotency scope/key:

- same key + same request fingerprint returns/reconstructs the established result;
- same key + different fingerprint is rejected.

### Database correctness

Critical invariants are enforced with PostgreSQL transactions, conditional transitions, unique/partial constraints, locking or equivalent DB mechanisms.

Application read-then-write checks are not sufficient for critical races.

### Inbox

A durable inbox deduplicates transport delivery by consumer/message identity.

Inbox dedupe does not replace business-key idempotency.

### Transactional outbox

Where a committed state change must cause an asynchronous event, the domain change and outbox event are committed in one PostgreSQL transaction.

The outbox publisher may deliver more than once. Consumers remain idempotent.

### External providers

Use a stable provider idempotency key derived from the logical local operation where supported.

If a provider does not offer suitable idempotency, persist durable local operation state and reconcile uncertain outcomes before repeating destructive calls.

## Contract

Tirodhan targets:

> at-least-once transport + exactly-once intended business effect

It does not claim magical distributed exactly-once network execution.

## Testing consequence

Every relevant mutating use case must test:

- success;
- exact replay;
- duplicate delivery;
- concurrent execution;
- crash/retry around DB commit;
- crash/retry around external side effects.

See `docs/IDEMPOTENCY.md` for the operation matrix.
