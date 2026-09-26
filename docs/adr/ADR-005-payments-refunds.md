# ADR-005: Payments and Refunds

**Status:** Accepted at architecture level; provider open

## Decision

Create the collection request before payment with state `PENDING_PAYMENT`.

Serviceability must be resolved before initiating payment.

Payment initiation is synchronous/customer-driven; the provider-hosted flow may redirect the user.

Provider webhook and/or server-to-server reconciliation is authoritative. Frontend return/timeout is UX only.

Successful payment moves the request to `ACCEPTED` and confirms the selected collection slot.

`PENDING_PAYMENT` has a configurable retry expiry, then becomes `EXPIRED`; physical deletion occurs only after a separate retention/reconciliation period.

Refunds are separate records/processes and are raised against the original successful provider payment reference. The provider returns funds to the original payment source.

## Invariants

- payment/refund webhook handling is idempotent;
- payment instrument details are never stored;
- refund status does not distort collection-request state;
- late provider callbacks remain reconcilable during the retention window.

## Open

- production payment gateway vendor;
- exact retry/retention timings.
