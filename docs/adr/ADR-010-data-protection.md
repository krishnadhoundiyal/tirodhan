# ADR-010: Data Protection

**Status:** Accepted at architecture level

## Decision

Use OWASP ASVS Level 2 / OWASP MASVS principles as the application/mobile baseline.

Selected direct PII is application-encrypted where doing so does not prevent required computation.

Mobile numbers use both:

- recoverable encrypted storage;
- a keyed non-reversible lookup representation for equality/uniqueness.

Exact lat/long remains queryable in PostgreSQL/PostGIS.

Never persist OTP values or payment instrument credentials.

Refresh tokens are not stored plaintext.

Provider secrets belong in Key Vault.

Azure-managed encryption at rest is sufficient for the MVP unless a later legal/contractual requirement justifies customer-managed keys.

## Logging/messaging

PII must not leak into logs.

Service Bus payloads should prefer identifiers over direct PII.

See `docs/DATA_PROTECTION.md`.
