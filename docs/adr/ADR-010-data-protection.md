# ADR-010: Data Protection

**Status:** Accepted at architecture level

## Decision

Use OWASP ASVS Level 2 / OWASP MASVS principles as the application/mobile baseline.

Selected direct PII is application-encrypted where doing so does not prevent required computation.

Mobile numbers use both:

- recoverable encrypted storage;
- a keyed non-reversible lookup representation for equality/uniqueness.

Full household-address snapshots remain protected at application level even when immutable historical transaction data.

Exact lat/long and other required spatial snapshots remain queryable in PostgreSQL/PostGIS.

Never persist OTP values or payment instrument credentials.

Refresh tokens are not stored plaintext.

Provider secrets belong in Key Vault.

Azure-managed encryption at rest is sufficient for the MVP unless a later legal/contractual requirement justifies customer-managed keys.

## Messaging/reliability data

Service Bus messages, inbox/outbox records, idempotency records, and provider-event records must not become alternate PII stores.

Prefer internal identifiers and minimal routing/reconciliation metadata.

Idempotency records store request fingerprints rather than copies of sensitive request bodies.

## Logging

PII, exact coordinates, tokens, OTPs, provider secrets and payment-sensitive data must not leak into logs.

See `docs/DATA_PROTECTION.md`.
