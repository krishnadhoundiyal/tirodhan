# ADR-007: Human Identity and Application Sessions

**Status:** Accepted at architecture level

## Decision

All human login uses mobile number + OTP only.

No username/password, email login, or social login for MVP.

Use one application user identity per human.

A user may hold multiple application roles:

- `CUSTOMER`
- `RIDER`
- `MANAGER`

Rider/manager roles require explicit provisioning/approval.

A user has one active verified login mobile number at a time. A phone-number change preserves the same application user and retains phone-identity history rather than creating another user.

OTP provider verifies the phone number; Tirodhan never persists OTP values.

After successful verification, Tirodhan issues its own session:

- short-lived access token;
- rotating/revocable refresh-token session.

Refresh tokens are stored only as cryptographic verifier/hash material, never plaintext.

Refresh-token rotation uses token-family semantics so reuse of an already rotated/revoked token can be detected and rejected/revoked according to security policy.

Access tokens are not stored as ordinary application data.

MSG91 is the current OTP-provider candidate; commercial confirmation/DLT onboarding remains a launch task.

## Security / idempotency

- repeated OTP/session commands must not create unintended duplicate sessions;
- unsafe refresh-token replay is rejected rather than treated as a successful idempotent replay;
- human identity is used for application authorization/audit, not Azure-resource authentication;
- phone numbers are normalized and stored with recoverable encryption plus keyed lookup representation according to `DATA_PROTECTION.md`.
