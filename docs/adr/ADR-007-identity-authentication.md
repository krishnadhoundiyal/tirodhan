# ADR-007: Human Identity and Application Sessions

**Status:** Accepted

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

- short-lived RS256 access JWT;
- stable opaque refresh credential generated with at least 256 bits of cryptographic randomness.

Persist only SHA-256 of the refresh credential. Do not persist its plaintext or a recoverable
encrypted copy. Access JWTs are not persisted.

Refresh credentials do not rotate. Refresh validates the same session and issues a new access JWT;
it does not create a successor, change the verifier, or extend the fixed configured session expiry.
One user may have multiple independently revocable active sessions.

If a successful refresh response is lost, retrying the same active credential succeeds. Possession
of a stolen active credential therefore grants refresh capability until explicit revocation, fixed
expiry, or user disable. Rotation, reuse detection, token families, device fingerprinting, and grace
windows are deliberately deferred for the MVP.

The initial login response has a different recovery policy. A completed `auth.verify` command stores
only its refresh-session ID, never bearer credentials. Exact replay does not call the OTP provider,
create another session, or mint replacement credentials; the client must start a new OTP login.

Access JWT claims are limited to `sub`, `sid`, `iss`, `aud`, `iat`, `exp`, and `typ=access`. Roles and
PII are excluded. Every protected request validates RS256, issuer, audience, type, times, and UUID
claims, then checks the live PostgreSQL session, user status, and active roles. Session revocation,
user disable, and role revocation take effect on the next request even before JWT expiry.

Concurrent refresh and logout serialize on the refresh-session row. Two refreshes may both succeed;
logout first makes refresh fail, while refresh first may return a JWT that becomes unusable as soon
as the following logout commits.

MSG91 is the current OTP-provider candidate; commercial confirmation/DLT onboarding remains a launch task.

## Security / idempotency

- OTP start retry convergence is the provider adapter's responsibility; no local database record can
  claim exactly-once SMS delivery;
- successful verification uses `auth.verify` command idempotency plus a phone-HMAC advisory lock and
  active-phone uniqueness so exact replay cannot create duplicate sessions or human identities;
- stable refresh retry/concurrency does not create or mutate sessions;
- logout is idempotent and unknown credentials receive generic success;
- human identity is used for application authorization/audit, not Azure-resource authentication;
- phone numbers are normalized and stored with recoverable encryption plus keyed lookup representation according to `DATA_PROTECTION.md`.
