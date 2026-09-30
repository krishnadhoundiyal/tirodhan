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
- rider personal/contact details;
- immutable booking/address snapshots containing the same direct PII.

Protect these at application level where doing so does not break required database computation.

Historical snapshots do not become less sensitive merely because they are immutable.

### Searchable mobile number

The system needs both:

- recoverable mobile number for legitimate communication;
- equality/uniqueness lookup.

Use two representations conceptually:

- recoverable encrypted value;
- keyed non-reversible lookup value such as an HMAC of the normalized number.

Do not use a plain unsalted/unkeyed hash of a phone number.

### Exact latitude / longitude

Exact coordinates remain queryable in PostgreSQL/PostGIS.

They are not application-encrypted because spatial indexes, proximity queries, geofence validation, and clustering require computational access.

Protect them through:

- PostgreSQL encryption at rest;
- TLS in transit;
- workload/DB authorization;
- restricted API access;
- no unnecessary propagation;
- retention controls;
- prohibition on telemetry logging.

`cell_id` remains queryable.

Historical handover/pickup coordinate snapshots receive the same access restrictions.

### Authentication data

- OTP values: never persist.
- Access tokens: do not persist as ordinary application data.
- Refresh credentials: at least 256 bits of opaque randomness; never store plaintext; persist only
  the SHA-256 verifier. They remain stable until fixed session expiry or explicit revocation and are
  never copied into logs, idempotency metadata, or recoverable encrypted storage.
- Mobile numbers: accept canonical E.164 only; persist a recoverable protected representation and a
  keyed deterministic HMAC for lookup. Never use plaintext or an unkeyed phone hash.
- Access JWTs use RS256 and contain no phone, roles, or other PII. Live database state remains the
  authorization authority.
- Provider/API secrets: Key Vault, not application tables.

Phase 1R phones use AES-256-GCM with a fresh 12-byte nonce, stable `tirodhan:user-phone:v1` AAD,
and a strict versioned envelope containing key ID, nonce and authenticated ciphertext/tag.
New encryption uses the active 32-byte AES key; retained key IDs decrypt historical envelopes.
Unknown key IDs, malformed envelopes and tampering fail closed. Lookup uses raw 32-byte
HMAC-SHA256 with an independent random 256-bit key, never the AES key or a derivative. HMAC-key
rotation is explicit maintenance/data migration, not transparent online multi-key lookup.
Missing/malformed crypto configuration has no plaintext or dummy fallback. Secret-aware settings
receive keys and Kaleyra API secrets through Key Vault -> ACA secret/reference -> process memory;
keys never enter Git, PostgreSQL, logs, error responses or container images.
AuthenticationChallenge contains only protected phone and provider binding metadata. OTP plaintext
and OTP hashes are never persisted, including reliability stores. Provider references stay server-side.

### Payment data

Never persist:

- PAN/card number;
- CVV;
- UPI PIN/credentials;
- bank credentials.

Persist only provider references and business data required for reconciliation/refunds, such as:

- provider;
- provider order/payment/refund/event IDs;
- amount;
- currency;
- state/timestamps.

Provider webhook payloads should not be retained wholesale merely for convenience. Persist the minimum metadata needed for deduplication, reconciliation, audit, and debugging.

### Media

Photos/videos:

- private Blob Storage;
- access only through authorized application flows;
- object names must not expose phone numbers, addresses, or other PII;
- local mobile copies awaiting upload remain app-private;
- lifecycle/retention policy applies.

Evidence metadata in PostgreSQL must remain minimal and should use internal IDs rather than descriptive PII.

Phase 1M media object keys are backend-generated as `media/<media_asset_id>` and contain no original
filename or user data. Short-lived upload authorization is returned transiently through the storage
port and must not be persisted or logged. PostgreSQL records only the expected content type and the
storage-reported content type, size, and finalization time; it never stores media bytes.

Phase 1Q enforces direct client-to-Blob upload mechanics utilizing transient Azure User Delegation SAS tokens.
FastAPI provides temporary write-only access to private Azure Blob containers for designated `media/<media_asset_id>` objects,
avoiding proxying media bytes through backend logs or runtime memory. Storage account keys are absent from application configuration.

## 3. Encryption at rest

Use Azure-managed encryption at rest for Azure services by default.

Do not introduce customer-managed keys/HSM infrastructure in the MVP unless a legal, contractual, or security requirement justifies the added complexity/cost.

Application-level encryption is reserved for selected direct PII rather than blindly encrypting every column.

The application-level encryption format must support key/version rotation.

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
- `pickup_execution_id`;
- `handover_event_id`;
- correlation/trace IDs.

## 6. Messaging and reliability metadata

Service Bus messages, outbox payloads, and inbox records should carry identifiers/control data wherever possible.

Do not place direct PII in queue/outbox payloads merely for convenience. Consumers should fetch authoritative data from PostgreSQL when appropriate.

`idempotency_record` must not become a copy of request bodies containing PII. Store a cryptographic request fingerprint plus the minimum result metadata required for replay.

`inbox_message` and `outbox_event` must not become alternate long-term business-data stores.

Provider-event records should store provider event identity/type and minimum reconciliation metadata rather than full sensitive payloads by default.

## 7. Mobile storage

Sensitive authentication material must use OS-provided secure storage/keystore facilities.

Pending media must remain inside app-private storage, not a public gallery, and should be removed after successful durable upload according to product policy.

Client-generated command/capture/media/handover IDs must be opaque identifiers and must not encode PII.

## 8. Authorization

Encryption does not replace authorization.

Application code must enforce:

- ownership rules;
- role rules;
- rider/manager provisioning status;
- request state-machine permissions;
- least-privilege workload access to data;
- access to historical snapshots/evidence only where operationally required.

Phase 1P derives every Rider and Manager command actor from the authenticated principal. Rider
assignment responses may include the immutable booking address snapshot and pickup location needed
for current work, but only for unreleased items on that Rider's active assignment. Released work is
excluded from the predecessor view. Manager rider, pending-group and incident lists omit household
addresses, exact household coordinates, phone/session data and payment data. Observed handover
coordinates and opaque media upload authorization are accepted/returned only where required and
must not be logged.

## 9. Retention

Retention durations remain to be finalized.

Separate:

- business/legal retention;
- payment/reconciliation windows;
- operational media retention;
- log retention;
- expired pre-payment request retention;
- idempotency-record retention;
- inbox/outbox retention;
- provider-event retention.

Physical deletion should occur only after the applicable retention/reconciliation window.
