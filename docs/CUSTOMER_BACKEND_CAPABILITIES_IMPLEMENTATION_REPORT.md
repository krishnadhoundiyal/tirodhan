# Customer backend capabilities implementation

26 October 2026. Backend implementation and local verification only. No frontend file,
cloud resource, deployed configuration or production flag is changed. No PR, merge or deployment.

## Starting authority

| Repository | Local path | Starting branch / clean HEAD | Fetched origin/main |
|---|---|---|---|
| Backend | `C:\Users\91956\Tirodhan\tirodhan` | `main`, `b281e03a47d202fd8b5c3d895075aa358062c5fa`, clean | Same commit |
| Frontend, read only | `C:\Users\91956\Tirodhan-frontend\tirodhan-frontend` | `main`, `f4d96d2b828cb39e4edfd282827a10ed07cab261`, clean | Same commit |

Both remotes were fetched before implementation. There was no divergence from the brief's
reference commits. Backend work starts from actual `origin/main` on
`codex/customer-backend-capabilities`; only that branch is eligible for commit/push.

Reviewed current router/dependencies, live identity/session/role admission, phone envelopes,
customer catalogue/read/cursor code, PostgreSQL/Alembic conventions, reliability primitives,
payment event producers, Rider device/FCM delivery, runtime logging and worker conventions.
Architecture context, domain/schema/relationships, data protection, idempotency and relevant
ADRs were consulted. Frontend review included all five requested contract/gap/parity/closure
documents; both customer API clients; Web's runtime decoder, browser notification port/service
worker; Mobile's capability gates, platform registration, signal decoder, lifecycle, and tests.
Older statements in `BACKEND_GAPS.md` about financial cancellation are historical; this change
does not alter the currently implemented financial rules.

## HTTP contract

All account and notification endpoints require the existing authenticated live CUSTOMER role.
No user/customer ID is accepted from the body. Private responses use `Cache-Control: private,
no-store`. UUIDs are strings over HTTP; server-created entity IDs are UUIDv7. Input objects
forbid unknown properties. Missing/invalid credentials are 401, non-CUSTOMER is 403.

| Method / path | Request | Successful response |
|---|---|---|
| GET `/v1/customer/me/profile` | None | 200 `{user_id,display_name:null|string,email:null|string,phone_display,version}` |
| PUT `/v1/customer/me/profile` | `{client_request_id:UUID,expected_version:int,display_name:null|string,email:null|string}` | 200 same profile DTO |
| GET `/v1/customer/me/preferences` | None | 200 `{language:"en-IN"|"hi-IN",collection_notifications:bool,version}` |
| PUT `/v1/customer/me/preferences` | `{client_request_id,expected_version,language,collection_notifications}` | 200 same preferences DTO |
| GET `/v1/customer/me/favourite-categories` | None | 200 `{category_codes:string[],version}` |
| PUT `/v1/customer/me/favourite-categories` | `{client_request_id,expected_version,category_codes}` | 200 same favourites DTO |
| POST `/v1/customer/feedback` | `{client_request_id,message}` | 201 `{feedback_id:UUID}` |
| GET `/v1/customer/payment-methods` | None | 200 `{methods:[]}`; no authoritative configured provider method discovery exists |
| GET `/v1/content/help` | Public, no credential | 200 `{slug,title,paragraphs:string[],updated_at:UTC,revision:int}` when published |
| GET `/v1/content/about` | Public, no credential | Same content DTO |
| GET `/v1/content/privacy` | Public, no credential | Same content DTO |
| GET `/v1/content/terms` | Public, no credential | Same content DTO |
| GET `/v1/customer/notifications?limit=20&cursor=…` | Limit 1–50, optional opaque cursor ≤2048 chars | 200 `{items:[{event_id,title,body,created_at,target:"COLLECTION",request_id}],next_cursor:null|string}` |
| PUT `/v1/customer/me/push-devices/{installation UUID}` | `{client_device_id:UUID,platform:"ANDROID",token_provider:"FCM",token:string}` | 200 `{client_device_id,registered_at:UTC}`, only with explicitly enabled valid FCM configuration |
| DELETE `/v1/customer/me/push-devices/{installation UUID}` | No body | 204, repeat/unknown/non-owned binding has no effect |
| PUT `/v1/customer/me/web-push-subscriptions/{installation UUID}` | **Direct `PushSubscriptionJSON`**, described below; exact approved `Origin` header | 200 `{client_device_id,registered_at:UTC}`, only with enabled valid Web Push configuration |
| DELETE `/v1/customer/me/web-push-subscriptions/{installation UUID}` | No body; exact approved `Origin` header | 204; works while delivery is disabled if the origin remains approved |

Profile name is null or trimmed 1–100 plain characters; email is null or a trimmed bounded
address ≤254 characters. Email is optional contact metadata, not a verified login credential.
Phone is masked from the existing active encrypted `user_phone` and is never editable here.
Missing phone/decryption configuration fails 503; no placeholder phone is fabricated.
Defaults without inserting rows on GET: empty profile, `en-IN`, notifications true,
empty favourites, each version 1. A first successful PUT produces version 2.

Favourite writes allow ≤500 distinct codes of 1–100 characters, checked under catalogue/group
share locks against active authoritative identities. Empty clears the set. Reads hide retired
categories/groups without rewriting a customer's version; exact older command replay retains
the original response even after retirement. The client joins current catalogue for selection.
Ordering is group display order/code, then category display order/code. No descriptions are copied.
Feedback is trimmed plain text 5–2000 characters. There is no feedback-list/read endpoint,
rating, support ticket or external messaging integration.

New domain errors use `{error:{code}}`: 409 `VERSION_CONFLICT`, `COMMAND_CONFLICT`,
`COMMAND_IN_PROGRESS`, `STALE_REGISTRATION`, `CURSOR_EXPIRED`; 422 `INVALID_CATEGORY`,
`DEVICE_ID_MISMATCH`, `INVALID_SUBSCRIPTION`; 403 `ORIGIN_NOT_ALLOWED`; 404 content not published;
503 missing crypto/lifetime/cursor/provider configuration. Account/body-validation failures
return only 422 `{error:{code:"INVALID_INPUT"}}`, without echoing sensitive request data.
Existing authentication error contracts remain unchanged. Unknown content slugs return 404.

The new router owns a transaction around response construction and commits before returning the
Response to ASGI. This avoids relying on FastAPI's version-dependent yield-dependency teardown
timing. A commit failure cannot send a success response; the regression test forces that boundary
and verifies rollback followed by clean same-command retry. Existing unrelated routers are unchanged.

## Controlled publication

The four routes are intentionally public and expose **only PUBLISHED** plain text.
They use public max-age 300, JSON response models and nosniff. All content starts absent;
there is no seeded legal/support copy or approval. Draft and missing content both fail public
reads with 404, while the database retains explicit DRAFT/PUBLISHED/RETIRED distinctions.

Existing live MANAGER admission controls these additional APIs:

| Method / path | Request | Response |
|---|---|---|
| POST `/v1/manager/content/{slug}/revisions` | `{client_request_id,expected_version:0-or-latest-revision,title,paragraphs}` | 200 `{revision_id,revision,status:"DRAFT"}` |
| POST `/v1/manager/content/{slug}/revisions/{revision_id}/publish` | `{client_request_id,expected_version:latest-revision,approval_reference}` | 200 `{revision_id,revision,status:"PUBLISHED"}` |

An initial draft expects version 0. Titles are 1–200 characters, paragraphs 1–50 ordered
entries of 1–4000 characters. Angle markup/control characters are rejected. Publication must
target the latest immutable draft and requires an explicit nonempty plain approval reference
of ≤200 characters. The manager's actual user ID, reference and server publication timestamp
are recorded. This records the authenticated manager's attestation; it does not independently
verify a legal approval document or require two distinct humans. Real legal/support approval
remains an owner prerequisite. A later publication atomically retires the prior revision.
Per-slug PostgreSQL advisory locks, version checks, partial unique published-slug index and SQL
immutable-text/lifecycle triggers arbitrate competing publishers. Exact command replay returns
the original result, including after a later publication.

## Persistence and privacy

Migration `0039_customer_capabilities` follows `0038_item_volume` and is additive:

- `customer_profile`, `customer_preferences`, `customer_favourite_set` and catalogue-FK membership;
- encrypted `customer_feedback` with unique `(user_id,client_request_id)`;
- `customer_command_receipt`, referencing existing idempotency record and owner, containing
  only an encrypted exact response snapshot for protocol replay, never current business authority;
- immutable `content_revision` / ordered `content_paragraph` and publication provenance;
- CUSTOMER-owned `customer_push_binding`, `customer_notification`, `customer_push_delivery`;
- composite parent uniqueness and ownership FKs for session/customer and collection/customer;
- history, active-installation/token, event-fan-out, page, and due-delivery indexes;
- immutable history/binding/content guards and session-revocation-to-binding trigger.

The profile remains an extension of `app_user`; there is no second identity/role/customer DB.
All phone authority remains in `user_phone`. No payment/accounting/dispatch/planning transition
is changed. No category taxonomy, payment method, government claim or phone number is seeded.
Existing rows need no backfill; defaults are projected until the first account mutation.

Independent customer-private AES-256-GCM versioned keys reuse the existing envelope primitive
with distinct AAD/magic. Purpose/owner binding protects profile, feedback, push secrets and replay
snapshots. Name/email are encrypted together; feedback and native/browser credentials are encrypted.
Independent keyed HMAC protects request fingerprints and token/endpoint equality; unkeyed phone
or profile hashes are not introduced. Browser endpoint identity is separate from credential-key
identity, so rotating subscription keys cannot create duplicate endpoint ownership. Raw tokens,
endpoints, key material, profile/feedback bodies and exact locations never enter outbox/inbox,
logs or ordinary idempotency metadata. The HTTP validation handler does not echo those inputs.

Retention durations remain an explicit owner decision. No automatic deletion policy or destructive
purge is invented. Configure command TTL through the existing setting; elapsed TTL is retention
metadata, not permission to repeat a committed command. Receipt/FK/history guards preserve replay
and evidence until controlled retention maintenance is approved. Key IDs must remain decryptable;
HMAC rotation requires explicit migration because equality/replay fingerprints use it. Keep these
keys independent of phone/address keys and from one another. Production release requires approved
profile/feedback, device tombstone, notification, publication and replay retention/access controls.
Populated new tables deliberately block downgrade; export/approved maintenance is required.

## Idempotency and concurrency

| Operation | Logical key and DB protection | Transaction / replay | External effect |
|---|---|---|---|
| Profile/preferences/favourites | `customer.<operation>:<actor>` + client UUID, keyed normalized fingerprint; existing unique command claim; user NO KEY UPDATE lock and version | Admission lock, claim, state, encrypted response receipt, completion commit together. Replay precedes version/catalogue revalidation and returns original DTO. Changed command conflicts; concurrent distinct writes at one version have one winner | None |
| Feedback | Same actor/client claim plus unique feedback command | Feedback, receipt and command completion atomic. Rollback leaves no feedback; lost-response replay returns same UUIDv7 | None |
| Draft/publication | Actor/client claim scoped to slug and target revision; slug lock, revision and unique published slug | Provenance/text/publication/replay receipt atomic; later versions do not change historical replay | None |
| Device PUT | Natural installation + endpoint/token identity; sorted advisory locks, session/role locks, partial global active uniqueness | Identical active credential/session replays registered_at; replacement revokes old rows and inserts immutable successor with a generation; newer session fences older login | No provider call |
| Device DELETE/logout | Owned installation + session; session row revocation trigger | 204 repeat; delayed older-session cleanup cannot delete a successor. Expired/revoked sessions are also checked at send | No provider call |
| Notification fan-out | Source outbox UUID; independent named inbox, unique source notification, unique notification/device delivery | History, per-device work and inbox completion are one DB transaction; rollback/redelivery creates no duplicate history | None in transaction |
| Push delivery | Notification + immutable binding/generation; due-row SKIP LOCKED and token/expiry lease | Claim commits before send; final result fences lease and generation. Crash/uncertainty retries same notification ID after lease expiry | Best-effort at-least-once signal; duplicate transport is possible and does not repeat business effects |

Device tombstones reject replay of retired credentials in the same login session. Re-enabling the
same retired credential requires a new login session or a fresh provider credential. The present
Mobile DTO has no monotonic client rotation counter: backend cannot distinguish two never-before-
seen tokens arriving out of order within the same session. Existing serialized Mobile lifecycle
is therefore required; a future stronger ordering contract would need an additive client generation.
An arbitrary new installation UUID is not hardware attestation. Possession of a token/subscription
and a live login is the registration boundary. Older-session fencing prevents delayed prior-account
callbacks from reclaiming a transferred installation or token. A claimed/in-flight provider
operation cannot be recalled on logout/preference change; subsequent claims recheck eligibility.

## Outbox and notification delivery

`python -m tirodhan.workers.customer_notifications` is a finite backend worker entry point.
No schedule, queue or cloud worker is provisioned by this change.

1. Select a bounded due batch of existing `CollectionRequestAccepted` and
   `CustomerFinancialStatusChanged` outbox facts using SKIP LOCKED and indexed inbox exclusion.
2. Validate the identifier-only envelope and resolve the real request/owner from PostgreSQL.
3. Under ordered customer page locks, persist safe generic history and per-device delivery work,
   then complete the consumer's inbox in the same transaction. Device iteration is streamed.
4. Claim due delivery in a short transaction; recheck preference, user/role, session expiry/revocation,
   current binding and generation. Decrypt only the selected credential. Send outside DB locks.
5. Persist SENT, permanent invalid/failure, suppression, disabled debt or bounded exponential retry.
   Lease is 60 seconds; send awaits ≤30 seconds; retry delay caps at 3600 seconds. Unknown failures
   leave recoverable claimed work and cause worker failure, not a false successful delivery.

This is an independent PostgreSQL outbox fan-out, following the existing regional consumer pattern.
It does **not** mark the shared source PUBLISHED or interfere with routing's consumption. No new
business event is added and no synchronous financial/booking send is introduced. First activation
can build history from retained real outbox facts; devices created after the source event are not
sent that old event. Existing sources do not emit customer completion/pickup/handover events; this
implementation does not pretend to notify every journey transition. Clients retain authoritative
API refresh/polling. Adding source coverage requires a separately scoped domain change.

History stays available when collection push is opted out. Preference is checked during fan-out
and again before each send; no marketing/legal override is invented. Generic Hindi or English
history copy follows service language at materialization. Full frontend translations are not added.
History IDs and request IDs support owned collection navigation; detail authorization remains in
the existing collection read. Order is history-created timestamp DESC, notification UUID DESC.
Signed cursors reuse the existing codec, with owner and `notifications` view, fixed snapshot/expiry,
tamper rejection and 409 expiry. A shared page lock and exclusive writer lock prevent an in-flight
uncommitted history insert from entering an already-issued snapshot. Creation timestamp means
durable notification materialization, not a claim that the user read it or the business event just occurred.

FCM reuses the real Firebase Admin adapter with a customer signal type, generic copy and a bounded
customer HTTP timeout. Per-token invalidity revokes only that immutable customer binding. Transient,
ambiguous, disabled and authentication errors retain retry debt. No Rider binding/identity is reused.

Web Push uses existing cryptography/PyJWT plus standard HTTPS, with
[RFC 8291 aes128gcm](https://www.rfc-editor.org/rfc/rfc8291) and
[RFC 8292 VAPID](https://www.rfc-editor.org/rfc/rfc8292). The RFC's exact published encryption
vector is tested. No new package/service is required.
Direct `PushSubscriptionJSON` body is `{endpoint,expirationTime:null|integer-milliseconds,
keys:{p256dh,auth}}`; omitted expirationTime is accepted. The path installation UUID is held by
the browser adapter; a native FCM token object is not accepted as a subscription. Endpoint is HTTPS,
≤2048 characters, no credentials/fragment/IP literal/nonstandard port/backslash/whitespace;
its exact origin must be approved. P-256 and 16-byte auth keys are validated. Browser origin must
match its independent exact allowlist. Delivery rechecks that origin. No permissive CORS is added;
the approved ingress must preserve and enforce browser Origin.

HTTPS sending resolves once, rejects any private/mixed DNS answer, pins the chosen global IP,
and uses the original hostname for TLS certificate validation/SNI/Host. It ignores proxy environment,
does not follow redirects, read provider response bodies, or log subscription URLs. DNS delay is
checked against a send deadline before socket creation. Status 404/410 revokes; 400/413 is permanent;
auth/rate-limit/server/network/redirect failures remain retryable. Payload is only numeric schema
version, notification event UUID, COLLECTION target and owned request UUID. No status, amount,
provider details, URL, household data, secrets or coordinates are sent.

## Compatibility and gates

Implemented account/content/history/feedback DTOs match the proposed common Mobile/Web surfaces.
Web's decoder tolerates the additive content `revision`; both clients still receive updated_at.
Payments safely return no available methods instead of inventing a provider catalogue.

Deliberately gated/unresolved facts:

- **iOS/APNS:** Mobile proposes IOS/APNS. Existing backend provider is FCM, which cannot send to
  a raw APNs device token. This valid pair returns 503; no false registration or raw-APNs-to-FCM
  conversion is attempted. APNs credentials/topic/environment and a verified adapter remain required.
- **FCM wire schema:** FCM data values must be strings, so the actual signal has `schema_version:"1"`.
  Mobile `collectionSignal` currently checks numeric `1`; it needs explicit strict FCM normalization
  before collection navigation. Foreground/fallback refresh remains safe. No end-to-end native
  delivery claim is made. Enable Mobile push only after that follow-up and actual device tests.
- **Browser integration:** Web currently injects a `WebPushPort` and has no concrete backend URL.
  Implement that port against the documented subscription routes, retain an installation UUID,
  pass exact Origin, and configure approved VAPID public key/service-worker scope. Its existing
  numeric JSON signal/service-worker envelope matches this Web Push adapter.
- **Content:** All four documents are initially missing. Real approved texts and manager provenance
  must be supplied before publication. No fixture is a legal/support approval.
- **Production:** No delivery adapter is enabled by default and no external call/resource is made.
  Provider/device/browser acceptance, approved origins/credentials and worker scheduling remain
  deployment dependencies. Local mock-provider tests do not establish real provider delivery.
- **Retention and rotation:** Approved durations/cleanup, key custody/rotation and device tombstone
  lifecycle remain operational inputs; no guessed deletion policy is shipped.
- **Existing regression failure:** The unchanged TwoFactor traceback-redaction assertion fails
  in the local Linux Python 3.13 image, including on the exact starting commit. It needs separate
  investigation; this work does not waive or modify that test/provider boundary.

The generic native push capability flag should remain off for the current Mobile release until
its FCM decoding and iOS decision are resolved. Common account/history capabilities may be enabled
independently only after deployment and environment acceptance. No frontend file was modified.

## Configuration and cost

Required for private data/mutations: `TIRODHAN_CUSTOMER_DATA_ACTIVE_KEY_ID`,
`TIRODHAN_CUSTOMER_DATA_ENCRYPTION_KEYS` (key-ID to base64 32-byte AES keys),
`TIRODHAN_CUSTOMER_DATA_HMAC_KEY` (independent base64 32-byte key), existing protected phone
configuration and `TIRODHAN_COMMAND_IDEMPOTENCY_TTL_SECONDS`. Cursor reads require existing
customer signing key/TTL. Keys must be supplied through the approved secret injection mechanism.

Native delivery additionally requires explicitly approved `TIRODHAN_CUSTOMER_FCM_ENABLED=true`,
matching FCM project and a valid JSON-object service-account configuration, never a file-path
string. The disabled API startup does not load Firebase Admin. Web additionally requires explicit
`TIRODHAN_CUSTOMER_WEB_PUSH_ENABLED=true`, nonempty `CUSTOMER_WEB_PUSH_ORIGINS` and
`CUSTOMER_WEB_PUSH_ENDPOINT_ORIGINS` JSON lists, P-256 `CUSTOMER_VAPID_PRIVATE_KEY_PEM` and approved
`CUSTOMER_VAPID_SUBJECT`. All names have the TIRODHAN_ prefix. Registration checks configuration;
credentials alone never enable delivery. No approved production resource/origin/key is fabricated.
The finite worker requires `TIRODHAN_OUTBOX_PUBLISH_BATCH_SIZE` within 1–500.

No Terraform/cloud topology, recurring resource or new paid service is introduced. Running the
finite worker consumes existing PostgreSQL/compute/provider usage; review its cadence and
production provider/privacy settings before deployment. Hosting/schedule approval is not inferred.

## Verification

Local verification uses disposable databases `tirodhan_customer_capabilities_test` (full suite)
and `tirodhan_customer_capabilities_focused` (isolated focused checks) on the existing local
PostGIS container, never an application or production database. They were
bootstrapped with PostGIS, upgraded from empty through all migrations, and empty-downgraded
to 0038/re-upgraded to 0039. Tests exercise populated downgrade refusal without data loss.

| Check | Result |
|---|---|
| Ruff lint | Passed, repository-wide |
| Ruff formatting | Passed, 565 Python files |
| Strict mypy | Passed, 265 source modules |
| Focused PostgreSQL / push suite | 70 passed in 61.24s on Python 3.10 / Windows |
| Additional Linux ownership-race and standalone-worker cases | 2 passed in 7.91s; same assertions and timeout, direct Docker/PostgreSQL network |
| Finite worker CLI | Also passed independently in a fresh Windows process with providers disabled; history and pending work persisted, zero send attempts |
| Invalid provider configuration boundary | Passed after the final JSON-object-only hardening |
| Migration | Empty-to-head and empty 0039 → 0038 → 0039 passed; final session-revocation index round trip passed; populated downgrade refusal passed |
| Full Linux regression suite | **1710 passed, 65 skipped, 1 failed**, 26 warnings, 1887.77s |
| Exact-baseline reproduction of the failure | `b281e03a47d202fd8b5c3d895075aa358062c5fa`, same Linux image: same OTP test failed in 3.43s |
| Same unchanged OTP test on local Python 3.10 | Passed in 4.49s |

The focused suite plus the two additional cases cover 72 distinct new test cases. Provider sends
are mocked; the RFC vector and real PostgreSQL behavior are not. The existing Firebase Admin
multicast API emits a deprecation warning for `tokens`; its delivery behavior is preserved.
The full regression snapshot contained 70 new cases; the two additional worker/ownership cases
were verified separately. The final configuration hardening and session index also received
their targeted checks. The suite is **not represented as fully green**: its only failure is
`tests/unit/test_twofactor.py::test_timeout_traceback_and_http_logging_hide_sensitive_url`
(line 164), reproduced from a clean archive of the exact original commit. Neither that test nor
`identity/twofactor.py` differs from the starting commit. Existing warnings also include routing
query cartesian-product and async-stream cleanup warnings. The 65 skips are the existing explicit
native-engine, graph and expensive benchmark opt-ins; no exclusion was added to achieve a pass.

Windows's full-suite collection failed because existing benchmark modules import Unix `resource`.
A Windows multi-connection check also intermittently failed during asyncpg connection bootstrap
with WinError 64; PostgreSQL showed no lock cycle. Final concurrent registration and CLI checks
passed on Linux with the original timeout/assertions. The full suite runs in the existing local
`tirodhan-routing-tests:increment-8` Linux image (Python 3.13.5, OR-Tools 9.14.6206) from a local
source snapshot, using `python -m pytest -q -p no:cacheprovider`. It uses the same isolated
PostgreSQL instance via the Docker bridge, avoiding Windows filesystem/proxy overhead. No
existing tests, markers or assertions are relaxed; native/expensive opt-in skips remain explicit.

New test inventory: `tests/integration/test_customer_capabilities.py` and
`tests/unit/test_customer_push.py`. Coverage includes defaults, HTTP field/status/cache contracts,
missing credentials and Rider/Manager denial, owner isolation, malformed private input redaction,
exact replay after later edits, changed-command conflict, simultaneous same/different commands,
rollback before commit and retry after lost responses, catalogue retirement and empty favourites,
encrypted persistence, duplicate outbox fan-out, owned deterministic cursor pages/expiry/tampering,
preference/role/session/generation suppression, token rotation/account switch/old-session cleanup,
native and browser registration/revocation, Web endpoint/origin/key validation, DNS pinning/private
answer refusal, VAPID signatures, RFC encryption bytes, provider-disabled behavior, transient and
invalid delivery, crash after external send/lease recovery, competing senders, draft/publication
approval, SQL immutable history/ownership/version constraints and populated downgrade safety.
No real Firebase/APNs/browser/financial provider request, checkout or deployed journey is tested.

## Changed files

- `.env.example`
- `docs/ARCHITECTURE.md`
- `docs/CUSTOMER_BACKEND_CAPABILITIES_IMPLEMENTATION_REPORT.md`
- `docs/DATA_PROTECTION.md`
- `docs/DOMAIN_MODEL.md`
- `docs/ER_DIAGRAM.md`
- `docs/IDEMPOTENCY.md`
- `docs/PROJECT_CONTEXT.md`
- `docs/SCHEMA_DESIGN.md`
- `migrations/env.py`
- `migrations/versions/0039_customer_capabilities.py`
- `src/tirodhan/api/router.py`
- `src/tirodhan/api/routes/customer_account.py`
- `src/tirodhan/core/config.py`
- `src/tirodhan/main.py`
- `src/tirodhan/modules/collection_requests/models.py`
- `src/tirodhan/modules/customer_account/__init__.py`
- `src/tirodhan/modules/customer_account/content.py`
- `src/tirodhan/modules/customer_account/devices.py`
- `src/tirodhan/modules/customer_account/models.py`
- `src/tirodhan/modules/customer_account/native_push.py`
- `src/tirodhan/modules/customer_account/notifications.py`
- `src/tirodhan/modules/customer_account/protection.py`
- `src/tirodhan/modules/customer_account/schemas.py`
- `src/tirodhan/modules/customer_account/service.py`
- `src/tirodhan/modules/customer_account/web_push.py`
- `src/tirodhan/modules/dispatch/push.py`
- `src/tirodhan/modules/identity/models.py`
- `src/tirodhan/modules/reliability/models.py`
- `src/tirodhan/workers/customer_notifications.py`
- `tests/integration/test_customer_capabilities.py`
- `tests/unit/test_customer_push.py`
