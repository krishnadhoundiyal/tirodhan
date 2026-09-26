# ADR-007: Human Identity and Application Sessions

**Status:** Accepted at architecture level

## Decision

All human login uses mobile number + OTP only.

No username/password, email login, or social login for MVP.

OTP provider verifies the phone number; Tirodhan then issues its own application session:

- short-lived access token;
- revocable refresh token.

Roles are application-owned:

- `CUSTOMER`
- `RIDER`
- `MANAGER`

Rider/manager roles require explicit provisioning/approval.

MSG91 is the current OTP-provider candidate; commercial confirmation/DLT onboarding remains an implementation/launch task.

## Security

- never persist OTP values;
- refresh tokens are not stored plaintext;
- phone numbers are normalized;
- human identity is used for application authorization/audit, not Azure-resource authentication.
