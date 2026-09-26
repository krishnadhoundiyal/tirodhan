# ADR-007: Human Identity and Application Sessions

**Status:** Accepted at architecture level; refresh-session retry/rotation semantics open

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
- revocable refresh-session mechanism.

Refresh credentials are never stored in plaintext. Access tokens are not stored as ordinary application data.

The exact refresh-session mechanism is deliberately not yet frozen. In particular, the design must explicitly handle the case where a refresh succeeds, the response is lost, and the client retries the previously presented credential.

The final design must define:

- revocation semantics;
- replay/reuse detection semantics;
- concurrency behaviour for simultaneous refreshes;
- whether and how a legitimate lost-response retry can recover without creating a second unintended session effect;
- the security/UX trade-off if an ambiguous retry is treated as credential reuse.

Possible mechanisms may include rotation with a bounded retry/grace strategy, a stable revocable refresh credential, or another reviewed design. None is approved merely by being listed here.

An implementation agent must not choose or encode the final mechanism until this open decision is resolved.

MSG91 is the current OTP-provider candidate; commercial confirmation/DLT onboarding remains a launch task.

## Security / idempotency

- repeated OTP/session commands must not create unintended duplicate sessions;
- refresh replay/concurrency must not create an unintended second business effect;
- ambiguous network retry versus credential theft/reuse semantics remain an explicit architecture decision, not an implementation default;
- human identity is used for application authorization/audit, not Azure-resource authentication;
- phone numbers are normalized and stored with recoverable encryption plus keyed lookup representation according to `DATA_PROTECTION.md`.
