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

Phase 1R selects Kaleyra Verify as the runtime implementation. Only transaction-bound providers
are supported: `start_verification(normalized_phone)` returns `provider_reference`, and
`verify(provider_reference, code)` verifies that transaction without a phone argument.
Tirodhan persists its own UUIDv7 AuthenticationChallenge; its public reference differs from the
provider reference, which is never exposed to clients. Phone is fixed at start; verify does not
accept phone. Provider credential state is separate from local `ACTIVE`, `CONSUMED`, `SUPERSEDED`
intent state. `CONSUMED` records a committed login. No OTP or OTP hash is persisted.
Kaleyra owns OTP generation, correctness, expiry, limits and single use. Local expiry is configured
and must be intentionally aligned with the provider flow. New starts use a new client request ID;
resend is not implemented. Missing runtime configuration fails closed without fallback.

The reusable asynchronous HTTP runtime has bounded timeouts and no automatic Generate/Validate
retry. Application-created clients close on shutdown; injected clients remain caller-owned.
Phone protection uses versioned AES-256-GCM envelopes and an independent HMAC-SHA256 lookup key.
Keys and provider secrets arrive through Key Vault -> Container Apps secret/reference -> process
configuration. Encryption key IDs support retained-key decryption; lookup-key rotation requires
an explicit maintenance/data migration.

Provider API references: [Generate OTPs](https://developers.kaleyra.io/docs/generating-otp) and
[Validate OTPs](https://developers.kaleyra.io/docs/validating-otp). Both use POST with `api-key`;
Generate returns `data.verify_id`, while Validate submits `verify_id` and `otp`. E910/E911/E912/E913
are authentication failures, never evidence of a committed application login.

### Phase 2 provider integration

The approved Phase 2 provider-integration request adds 2Factor alongside Kaleyra through the same
transaction-bound port. `TIRODHAN_OTP_PROVIDER` explicitly selects `2FACTOR` or `KALEYRA_VERIFY`;
credentials alone do not select a provider. There is no automatic fallback. Existing Kaleyra
deployments must now explicitly select `KALEYRA_VERIFY`. An unselected runtime fails closed on use;
incomplete selected configuration fails during startup. A stored challenge's provider code must
match the current runtime before verification can call the provider.

2Factor uses the approved legacy GET AUTOGEN/VERIFY pair at fixed `https://2factor.in`, with an
optional account-approved template. Only `Status=Success` and `Details=OTP Matched` verify a login.
Provider references remain private. Path credentials require suppressed HTTP URL logging,
sanitized exceptions without transport chaining, bounded timeouts and disabled redirects/retries.
The local intent lifetime remains explicit product configuration; provider expiry and detailed
error taxonomy must be confirmed with 2Factor before nonprod acceptance. Newer header APIs are not
combined with the legacy verifier. Sources, operational prerequisites and limitations are recorded
in [the provider integration report](../PHASE_2_PROVIDER_INTEGRATION_REPORT.md).

## Security / idempotency

- OTP start first commits an `auth.start` reservation keyed by client request ID and fingerprinted
  with that UUID plus phone HMAC only. A different fingerprint conflicts; IN_PROGRESS returns 409
  without provider invocation; COMPLETED loads its result challenge and replays only while ACTIVE
  and unexpired. Only the claim creator calls Generate outside a PostgreSQL transaction, then local
  supersession/challenge creation and start completion commit together. Phone advisory locking,
  row locking and partial uniqueness serialize supersession; challenge client-ID uniqueness remains
  a domain backstop. Different keys may each send, but simultaneous same-key calls cannot both send.
  Ambiguous provider failures and post-invocation local failures retain IN_PROGRESS; no automatic
  reclamation/retry is permitted, even when expiry metadata has elapsed. Known local failures before
  provider invocation roll back the reservation. Recovery requires a new client request ID;
- verify checks completed login replay before provider invocation, then locks and revalidates the
  challenge after provider success. Supersession or consumption during the call prevents session
  creation. Challenge consumption, identity, session and login idempotency complete atomically;
- provider success followed by local crash may leave a locally ACTIVE intent with an externally
  consumed OTP. Start a new attempt; no recovery state or distributed transaction is introduced;
- successful verification uses `auth.verify` command idempotency plus a phone-HMAC advisory lock and
  active-phone uniqueness so exact replay cannot create duplicate sessions or human identities;
- stable refresh retry/concurrency does not create or mutate sessions;
- logout is idempotent and unknown credentials receive generic success;
- human identity is used for application authorization/audit, not Azure-resource authentication;
- phone numbers are normalized and stored with recoverable encryption plus keyed lookup representation according to `DATA_PROTECTION.md`.
