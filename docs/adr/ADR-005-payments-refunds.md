# ADR-005: Payments and Refunds

**Status:** Accepted at architecture level; provider open

## Decision

Create the collection request before payment with state `PENDING_PAYMENT`.

Serviceability must be resolved before initiating payment.

Each CollectionRequest has one logical Payment obligation.

Gateway/provider retries are separate append/history PaymentAttempts under that logical Payment.

Payment initiation is synchronous/customer-driven; the provider-hosted flow may redirect the user.

Provider webhook and/or server-to-server reconciliation is authoritative. Frontend return/timeout is UX only.

Provider events are durably deduplicated by provider event identity.

A successful attempt, logical Payment success, request transition `PENDING_PAYMENT -> ACCEPTED`, and required outbox event are persisted atomically.

`PENDING_PAYMENT` has a configurable retry expiry, then becomes `EXPIRED`; physical deletion occurs only after a separate retention/reconciliation period.

Refund is a separate financial entity/process raised against the actual successful provider charge/reference. Refund status is independent from CollectionRequest state.

The schema permits multiple/partial refunds even if MVP normally performs full refunds.

## Duplicate external success

Asynchronous payment races may cause more than one provider attempt to report success.

The system records the external truth. Only one successful attempt satisfies the logical Tirodhan Payment.

Any additional successful charge becomes a reconciliation/refund condition. It must not:

- accept the collection request again;
- create duplicate downstream events;
- be hidden merely to satisfy a DB uniqueness assumption.

## Idempotency/reconciliation

- payment/refund provider calls use stable provider idempotency keys where supported;
- payment/refund webhooks are idempotent;
- uncertain external outcomes are reconciled rather than blindly replayed;
- concurrent refund creation must not allow total committed refunds to exceed the original successful payment;
- payment instrument credentials are never stored.

## Open

- production payment gateway vendor;
- exact retry/retention timings.
