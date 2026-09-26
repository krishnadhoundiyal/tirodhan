# Data Protection

## 1. Baseline

Use OWASP ASVS Level 2 principles for the backend/application and OWASP MASVS principles for mobile clients.

The goal is data minimization, least privilege, secure storage/transit, safe authentication/session handling, controlled retention, and prevention of sensitive-data leakage.

## 2. Data classification

### Direct PII requiring application-level protection

Examples:

- customer name;
- customer mobile number;
- full household address;
- rider personal/contact details.

Protect these at application level where doing so does not break required database computation.

### Searchable mobile number

The system needs both:

- recoverable mobile number for legitimate communication;
- equality/uniqueness lookup.

Use two representations conceptually:

- recoverable encrypted value;
- keyed non-reversible lookup value (e.g. HMAC of normalized number).

Do not use a plain unsalted/unkeyed hash of a phone number as the lookup representation.

### Exact latitude / longitude

Exact coordinates remain queryable in PostgreSQL/PostGIS.

They are not application-encrypted because spatial indexes, proximity queries, and clustering require computational access.

Protect them through:

- PostgreSQL encryption at rest;
- TLS in transit;
- workload/DB authorization;
- restricted API access;
- no unnecessary propagation;
- retention controls;
- prohibition on telemetry logging.

`cell_id` remains queryable.

### Authentication data

- OTP values: never persist.
- Access tokens: do not persist as ordinary application data.
- Refresh tokens: never store plaintext; store only a verifier/hash suitable for revocation/rotation.
- Provider/API secrets: Key Vault, not application tables.

### Payment data

Never persist:

- PAN/card number;
- CVV;
- UPI PIN/credentials;
- bank credentials.

Persist only provider references and business data required for reconciliation/refunds, such as:

- provider;
- provider order/payment/refund ID;
- amount;
- currency;
- state/timestamps.

### Media

Photos/videos:

- private Blob Storage;
- access only through authorized application flows;
- object names must not expose phone numbers, addresses, or other PII;
- local mobile copies awaiting upload remain app-private;
- lifecycle/retention policy applies.

## 3. Encryption at rest

Use Azure-managed encryption at rest for Azure services by default.

Do not introduce customer-managed keys/HSM infrastructure in the MVP unless a legal, contractual, or security requirement justifies the added complexity/cost.

Application-level encryption is reserved for selected direct PII rather than blindly encrypting every column.

## 4. Data in transit

All external and service traffic must use TLS/HTTPS or the secure protocol required by the managed service.

Never send plaintext secrets or authentication material over unencrypted transport.

## 5. Logging and observability

Never log:

- mobile numbers;
- full addresses;
- exact latitude/longitude;
- access/refresh tokens;
- OTP values;
- provider secrets;
- payment instrument data;
- unredacted sensitive request/response bodies.

Prefer internal identifiers:

- `user_id`;
- `request_id`;
- `planning_batch_id`;
- `collection_group_id`;
- `assignment_id`;
- correlation/trace IDs.

## 6. Messaging

Service Bus payloads should carry identifiers/control data wherever possible.

Do not place direct PII in queue messages merely for convenience. Consumers should fetch authoritative data from PostgreSQL when appropriate.

## 7. Mobile storage

Sensitive authentication material must use OS-provided secure storage/keystore facilities.

Pending media must remain inside app-private storage, not a public gallery, and should be removed after successful durable upload according to product policy.

## 8. Authorization

Encryption does not replace authorization.

Application code must enforce:

- ownership rules;
- role rules;
- rider/manager provisioning status;
- request state-machine permissions;
- least-privilege workload access to data.

## 9. Retention

Retention durations remain to be finalized.

Separate:

- business/legal retention;
- retry/reconciliation windows;
- operational media retention;
- log retention;
- expired pre-payment request retention.

Physical deletion should occur only after the applicable retention/reconciliation window.
