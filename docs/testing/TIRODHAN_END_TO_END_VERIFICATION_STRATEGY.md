**TIRODHAN**

**End-to-End Verification, Security & Release Test Strategy**

Customer Mobile + Backend APIs + Azure + PostgreSQL + Async Workers +
External Providers

| **Purpose.** Define a repeatable, evidence-driven verification programme for Tirodhan that reaches manual-tester depth while using automation aggressively. The strategy covers functional journeys, API correctness, database effects, async processing, security, application data leakage, network behaviour, resilience, performance, privacy, and release acceptance. |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **Field**             | **Value**                                               |
|-----------------------|---------------------------------------------------------|
| Document status       | Working verification baseline                           |
| Primary environment   | Azure nonprod                                           |
| Primary mobile target | Customer Mobile (React Native / Expo development build) |
| Backend               | FastAPI modular monolith + workers/jobs                 |
| Database              | Azure PostgreSQL Flexible Server + PostGIS              |
| Test philosophy       | Automation-first, human-exploratory release gate        |
| Prepared              | 2026-10-08                                              |

# Contents

> 1\. Objectives and test philosophy
>
> 2\. System under test
>
> 3\. Prerequisites and environment readiness
>
> 4\. Test tooling and responsibilities
>
> 5\. Test data and nonprod controls
>
> 6\. Functional end-to-end suites
>
> 7\. API and contract verification
>
> 8\. Database and persistence verification
>
> 9\. Async workers, queues and exactly-once business effects
>
> 10\. Security verification
>
> 11\. Application leak and local-data verification
>
> 12\. Network and transport verification
>
> 13\. Performance and optimization verification
>
> 14\. Resilience, recovery and failure injection
>
> 15\. Mobile-native/device verification
>
> 16\. Accessibility and usability verification
>
> 17\. Observability and audit evidence
>
> 18\. Automation architecture
>
> 19\. Manual exploratory testing
>
> 20\. Test execution phases
>
> 21\. Defect severity and release gates
>
> 22\. Evidence pack and sign-off
>
> Appendix A. Master traceability matrix
>
> Appendix B. High-risk adversarial scenarios
>
> Appendix C. Suggested nonprod test identities and datasets

# 1. Objectives and test philosophy

The objective is not merely to prove that an API returns HTTP 200 or
that the mobile screen renders. The objective is to prove that a real
customer action produces the correct durable business result across the
mobile app, API, database, asynchronous workers, external providers and
customer-visible read models.

| **Core rule.** For every important journey, verify three layers: mobile outcome + API contract + authoritative backend effect. |
|--------------------------------------------------------------------------------------------------------------------------------|

- **Functional correctness —** complete customer and operational
  journeys including unhappy paths and concurrency races.

- **Security —** authentication, authorization, token lifecycle,
  secrets, transport, API abuse controls and privacy.

- **Leak resistance —** no sensitive information in logs, app storage,
  crash files, caches, screenshots, notification payloads or packaged
  artifacts.

- **Network correctness —** timeouts, retries, dropped connections,
  duplicate requests, offline/online transitions and TLS behavior.

- **Reliability —** idempotency, outbox/inbox, worker replay, scheduler
  races, provider uncertainty and recovery.

- **Performance —** API latency, mobile responsiveness, memory, image
  delivery, list rendering and database hot paths.

- **Evidence —** each release gate must be backed by reproducible logs,
  automation output or captured manual evidence.

## 1.1 Automation vs human testing

Automation should carry the bulk of repeatable regression. A human
tester remains valuable for exploratory, sensory and device-specific
validation, but should not be the only source of confidence.

| **Layer**                    | **Primary mechanism**                             | **Human role**                          |
|------------------------------|---------------------------------------------------|-----------------------------------------|
| Backend unit/integration     | Pytest + deployed API checks                      | Review failures and edge cases          |
| API contract                 | OpenAPI/schema assertions + scripted HTTP tests   | Spot-check business semantics           |
| Mobile component/integration | Jest + React Native Testing Library               | Review interaction quality              |
| Native E2E                   | Maestro                                           | Observe high-risk flows on real devices |
| Security                     | MobSF + Burp/mitmproxy + targeted dynamic testing | Manual exploit-oriented verification    |
| Exploratory/UAT              | Human tester                                      | Mandatory pre-release pass              |

# 2. System under test

## 2.1 Customer-facing scope

- OTP login, refresh, logout and role admission.

- Address creation/update/archive, map/pin interaction, current-location
  option and far-away-address confirmation.

- Serviceability context creation and resolution.

- Catalogue/remote image retrieval and recommendations based on prior
  collections.

- Pickup slot discovery.

- Collection draft, inline item add/remove/edit, review, authoritative
  quote and booking creation.

- Payment attempt creation, provider checkout and authoritative
  payment-state read.

- Active bookings, history, journey/detail views.

- Cancellation eligibility, cancellation race handling, backend-owned
  compensation and customer-visible refund status.

- Push notifications as signals followed by authoritative refetch.

- Account, addresses, preferences and customer-visible legal/support
  areas.

## 2.2 Backend / operational scope

- PostgreSQL/PostGIS schema, constraints and migrations.

- H3 serviceability/cell derivation and (cell, slot) planning work
  units.

- Planning/compaction, immutable batches and singleton fallback.

- Dispatch/assignment and rider lifecycle.

- Pickup execution, incidents, reassignment and receiving-point
  handover.

- Evidence/media registration/finalization and completion rule.

- Payment/refund lifecycle, webhooks and reconciliation.

- Outbox/inbox durability, Service Bus consumers and scheduled jobs.

- Blob-backed media/logging, Fluent Bit delivery and retention jobs.

# 3. Prerequisites and environment readiness

No end-to-end test run is considered valid until the following
prerequisites are explicitly checked.

| **Prerequisite**              | **Acceptance condition**                                                              | **Gate**                              |
|-------------------------------|---------------------------------------------------------------------------------------|---------------------------------------|
| Azure subscription/nonprod RG | Terraform apply successful; expected resources created                                | BLOCKER                               |
| PostgreSQL Flexible Server    | Reachable from ACA/runtime identities                                                 | BLOCKER                               |
| Database schema               | Migration ACA Job executed; alembic_version at head; tables/constraints present       | BLOCKER                               |
| PostGIS                       | Extension enabled and usable                                                          | BLOCKER                               |
| Backend image                 | SHA-tagged image deployed                                                             | BLOCKER                               |
| Workers/jobs                  | Required workers and scheduled jobs deployed/enabled                                  | BLOCKER                               |
| Service Bus                   | Queues/subscriptions/RBAC ready for tested flows                                      | BLOCKER                               |
| Key Vault                     | All required runtime secrets injected through managed identity/secret refs            | BLOCKER                               |
| GCP Maps Platform             | Billing enabled; required geocoding/map APIs enabled; separate restricted credentials | BLOCKER for compaction/serviceability |
| OTP provider                  | Exotel ExoVerify or Kaleyra real nonprod integration available for certification      | BLOCKER for real-auth E2E             |
| Razorpay                      | Test-mode account/keys + webhook configuration                                        | BLOCKER for payment E2E               |
| Blob                          | Remote catalogue/media/log containers available with intended access policy           | BLOCKER for asset/media paths         |
| Mobile development build      | Android/iOS build points to nonprod API                                               | BLOCKER                               |
| Test identities/data          | Known users, addresses, serviceability outcomes, payment/refund scenarios             | BLOCKER                               |

## 3.1 Database deployment distinction

| **Important.** Terraform provisions PostgreSQL infrastructure and the tirodhan database. Application tables are created by Alembic migrations. The migration Container App Job must be run separately and verified before API testing. |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

1\. terraform plan/apply completes with reviewed output.

2\. Migration image is available and the migration ACA Job points to the
intended SHA/tag.

3\. Run the migration job manually for the deployment.

4\. Verify migration job exit code/logs.

5\. Verify alembic_version equals repository head.

6\. Verify expected tables, indexes, checks, foreign keys and PostGIS
extension.

7\. Only then start API/application smoke tests.

# 4. Test tooling and responsibilities

| **Tool**                             | **Role**                            | **Use**                                                                                |
|--------------------------------------|-------------------------------------|----------------------------------------------------------------------------------------|
| Maestro                              | Primary black-box native E2E runner | Tap/type/assert/relaunch/deep links/customer journeys; CI regression                   |
| Jest + React Native Testing Library  | Mobile component/integration        | State, auth/session, forms, query invalidation, error states                           |
| Pytest                               | Backend unit/integration            | Domain invariants, concurrency, provider adapters, DB effects                          |
| HTTP scripted tests                  | Deployed API verification           | Status/headers/schema/error/idempotency against Azure nonprod                          |
| OpenAPI validation                   | Contract drift                      | Request/response compatibility and endpoint inventory                                  |
| MobSF                                | Static/dynamic mobile security      | APK/IPA secrets, permissions, exported components, weak storage indicators             |
| Burp Suite or mitmproxy              | Network/security proxy              | Inspect HTTPS calls, headers, payloads, retries, leakage, malformed input              |
| ADB / Android Studio tools           | Android diagnostics                 | Logcat, app storage, network state, memory, ANR/crash inspection                       |
| Xcode/Instruments (later)            | iOS diagnostics                     | Memory, network, keychain, lifecycle and native behavior                               |
| Frida/Objection (targeted, optional) | Deep dynamic security               | Runtime inspection when static/proxy testing indicates risk                            |
| Jules / code-review agent            | Test-analysis support               | Generate adversarial cases, review coverage, inspect failures; not execution authority |
| Human exploratory tester             | Release acceptance                  | Usability, device feel, visual defects, unexpected sequence exploration                |

## 4.1 Manual tester augmentation

The recommended model is not 'automation instead of a tester'. It is
automation as the tester's force multiplier. The manual tester receives
deterministic fixtures, repeatable Maestro flows, network manipulation
tools and a traceability matrix, allowing effort to focus on exploratory
and perceptual defects.

# 5. Test data and nonprod controls

Nonprod must provide deterministic test control without introducing
production backdoors. Controls should be environment-gated and
impossible to enable in prod.

- **OTP —** real-provider certification numbers plus a separate
  deterministic adapter for repeatable automated regression.

- **Payments —** Razorpay test mode; known success/failure/pending
  scenarios; never real money.

- **Serviceability —** known serviceable, unserviceable and
  resolver-failure addresses.

- **Planning —** slots far enough in the future plus controlled
  scenarios near cutoff.

- **Cancellation —** one cancellable ACCEPTED request, one
  cutoff-reached request, one already-cancelled request.

- **Refund —** pending, processing, submitted, succeeded, failed and
  uncertain/reconciliation scenarios.

- **History —** customers with no history, small history, paginated
  history and recommendation-producing history.

- **Notifications —** known test device registrations and replayable
  notification events.

- **Media —** valid image, oversized image, unsupported type,
  interrupted upload and stale authorization scenarios.

| **Production-safety requirement.** Any deterministic test-only provider, reset endpoint or fixture loader must be compiled/configured so it cannot be activated in production. |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 6. Functional end-to-end suites

Each scenario must record: test ID, input identity/data, device/build,
request IDs, expected UI state, API response, DB effect, async effect,
evidence and final pass/fail.

| **ID**        | **Scenario**          | **Expected end-to-end result**                                                             |
|---------------|-----------------------|--------------------------------------------------------------------------------------------|
| E2E-AUTH-01   | OTP happy path        | Start OTP → receive real SMS → verify → session established → /auth/me CUSTOMER → Home     |
| E2E-AUTH-02   | Invalid/expired OTP   | Wrong/expired code → clear customer-safe error; no session issued                          |
| E2E-AUTH-03   | Session refresh       | Expire access token → protected request → single refresh → request retried once            |
| E2E-AUTH-04   | Revoked refresh       | Revoke session → next refresh fails → app returns to login and clears state                |
| E2E-ADDR-01   | Saved address         | Create → list → edit → default → archive; verify versions and ownership                    |
| E2E-ADDR-02   | Map/current location  | Permission → map pin → reverse/geocode address → save; deny permission path also works     |
| E2E-ADDR-03   | Far-away warning      | Selected address \> threshold from current coordinate → non-blocking confirmation          |
| E2E-SVC-01    | Serviceable           | Address → serviceability PENDING → SERVICEABLE + cell_id; UI proceeds                      |
| E2E-SVC-02    | Unserviceable         | UNSERVICEABLE → customer cannot book; correct reason/state                                 |
| E2E-SVC-03    | Technical failure     | TECHNICAL_FAILURE → retry/recovery path; no false unserviceable result                     |
| E2E-CAT-01    | Catalogue             | Remote groups/categories/images loaded, ordered, cached, failures graceful                 |
| E2E-REC-01    | Recommendations       | History-backed ranked categories show under approved heading; absent data hides section    |
| E2E-BOOK-01   | Draft/edit            | Add items → Review → remove/add inline → address/slot retained                             |
| E2E-SLOT-01   | Slot availability     | Serviceability context → available slots → selection; stale slot rejected authoritatively  |
| E2E-BOOK-02   | Create collection     | Review → create → backend quote/status/payment obligation → appears Active                 |
| E2E-PAY-01    | Payment success       | Create attempt → Razorpay test checkout → webhook authoritative → ACCEPTED                 |
| E2E-PAY-02    | Payment uncertain     | Client callback/return without authoritative success → UI remains pending and refetches    |
| E2E-PAY-03    | Payment failure/retry | Failed attempt → retry creates allowed new attempt; logical payment invariant preserved    |
| E2E-ACT-01    | Active journey        | Booking appears with correct current milestone; detail reads authoritative state           |
| E2E-HIST-01   | History pagination    | Completed/cancelled records paginate deterministically and retain sort order               |
| E2E-CAN-01    | Cancellation allowed  | Backend flag true → confirm → one cancel command → CANCELLED → refund intent when required |
| E2E-CAN-02    | Cancellation race     | UI previously allowed; planner wins before POST → 409 → refetch → cancel disappears        |
| E2E-REF-01    | Refund progress       | CANCELLED + refund pending/processing → customer-safe status                               |
| E2E-REF-02    | Refund success        | Webhook/worker success → detail/history update → optional push → authoritative refetch     |
| E2E-NOTIF-01  | Push signal           | Tap notification → expected screen/resource → refetch authoritative state                  |
| E2E-NOTIF-02  | Duplicate push        | Duplicate signal does not duplicate business state or produce unsafe navigation loops      |
| E2E-LOGOUT-01 | Logout                | Backend revocation attempt + local clear; cache/draft/token/notification state removed     |

## 6.1 Planning / compaction end-to-end verification

Because geocoding exists principally to enable geographic compaction,
the compaction chain must be verified as a first-class business flow.

1\. Create multiple ACCEPTED requests in the same H3 resolution-7 cell
and slot.

2\. Create at least one request in an adjacent/different cell and the
same slot.

3\. Verify geocoding/location resolution produces expected coordinates
and H3 cell IDs.

4\. Run/trigger the planning compaction job at the configured lead time.

5\. Verify only eligible ACCEPTED rows for the work unit transition
through PRE_PLANNING/PLANNED.

6\. Verify immutable batch identity for (cell, slot).

7\. Verify grouping obeys configured compaction distance/max group size
and singleton fallback.

8\. Race cancellation against planning and prove only one winner
produces the business effect.

9\. Crash/replay the planning execution and verify idempotent
recovery/no duplicate batch membership.

# 7. API and contract verification

- Enumerate all existing and proposed Customer Mobile APIs from the
  frontend contract document and backend OpenAPI.

- For each route:
  method/path/auth/role/headers/request/response/status/error/idempotency/nullable
  fields.

- Generate positive, boundary and invalid-schema cases.

- Verify 401 vs 403 semantics; do not collapse authorization and
  authentication.

- Verify ownership: customer A cannot read/mutate customer B resources.

- Verify retry semantics with the same prepared idempotency key for the
  same user intent.

- Verify duplicate/replayed requests return established business result
  without duplicate effect.

- Verify 409 race/conflict responses are customer-recoverable.

- Verify errors do not leak stack traces, SQL, provider secrets or
  internal IDs beyond approved identifiers.

## 7.1 Minimum API negative matrix

| **Case**                                               | **Expected**                                       |
|--------------------------------------------------------|----------------------------------------------------|
| Missing Authorization                                  | 401                                                |
| Invalid/expired access token                           | 401                                                |
| Authenticated wrong role                               | 403                                                |
| Resource belongs to another user                       | 404 or approved non-leaking authorization behavior |
| Malformed UUID                                         | 422/contract validation                            |
| Missing required idempotency header on legacy endpoint | 400/422 as documented                              |
| Same idempotency key + same body                       | same established result                            |
| Same idempotency key + different body                  | 409 conflict                                       |
| Unsupported state transition                           | 409                                                |
| Unknown resource                                       | 404                                                |
| Provider unavailable                                   | 503/customer-safe response                         |
| Rate-limited OTP                                       | 429                                                |
| Oversized/invalid input                                | 422 or documented validation                       |

# 8. Database and persistence verification

- Verify every migration applies from an empty database and from
  previous supported revision.

- Verify alembic downgrade policy explicitly (even if rollback is
  forward-fix only).

- Verify all foreign keys, unique constraints, check constraints and
  partial indexes required for business invariants.

- Verify UUIDv7 primary/business IDs where intended.

- Verify phone numbers are encrypted/HMACed as designed; no plaintext
  search columns.

- Verify exact coordinates are not copied into logs/audit payloads.

- Verify idempotency, outbox and inbox rows have appropriate
  retention/status.

- Verify transaction boundaries for payment success + ACCEPTED
  transition.

- Verify cancellation and future refund-intent creation are durably
  coordinated in backend.

- Verify completion requires required pickup/handover/evidence
  conditions and is idempotent.

## 8.1 Database assertion pattern

For high-risk commands, the automated E2E suite should optionally query
a read-only test-validation connection or test-only verification
endpoint after the user-visible assertion.

| **Command**           | **Authoritative persistence assertion**                                                                       |
|-----------------------|---------------------------------------------------------------------------------------------------------------|
| Create address        | UserAddress ACTIVE, correct user/version/default invariant                                                    |
| Serviceability create | Context PENDING, snapshot persisted, outbox requested                                                         |
| Payment captured      | Payment SUCCEEDED + canonical attempt + request ACCEPTED atomically                                           |
| Cancel                | Request CANCELLED, cancelled_at set, no planning batch; required refund intent/outbox present when applicable |
| Refund                | Refund status and provider reference consistent; no over-refund                                               |
| Planning              | Request batch membership + immutable batch + no duplicate membership                                          |
| Complete              | Exactly required COLLECTED/VALIDATED evidence conditions and completed_at once                                |

# 9. Async workers, queues and exactly-once business effects

Service Bus delivery is at-least-once. Tests must therefore prove
duplicate transport delivery does not create duplicate business effects.

- Duplicate same message ID.

- Duplicate business event with different transport delivery where
  contract permits.

- Worker crash after provider call but before local status update.

- Worker crash after local status update but before message completion.

- Poison/malformed message dead-letter behavior.

- Transient provider timeout/uncertain outcome.

- Queue redelivery after visibility/peek-lock timeout.

- Inbox conflict detection.

- Outbox publish replay.

- Scheduled job overlap or double-trigger.

| **Acceptance principle.** At-least-once transport is acceptable only if externally visible business effects remain exactly once. |
|----------------------------------------------------------------------------------------------------------------------------------|

# 10. Security verification

Security testing is a release gate, not an optional audit. Use OWASP
MASVS/MSTG concepts as the organizing checklist.

| **Domain**   | **Must verify**                                                                            |
|--------------|--------------------------------------------------------------------------------------------|
| AUTH         | OTP abuse, replay, brute force/rate limiting, challenge ownership, refresh revocation      |
| AUTHZ        | CUSTOMER/RIDER/MANAGER separation, object ownership, endpoint role enforcement             |
| SESSION      | Memory-only access token, SecureStore refresh, logout/revocation, user switching           |
| API          | BOLA/IDOR, mass assignment, invalid transitions, header manipulation, injection            |
| TRANSPORT    | HTTPS-only, certificate validation, no cleartext endpoints, secure redirect handling       |
| SECRETS      | No server secret/API credential embedded in mobile bundle; restricted mobile map keys only |
| STORAGE      | No plaintext token/PII in AsyncStorage/files/cache/backups/logs                            |
| LOGGING      | No phone, OTP, tokens, coordinates, provider secrets or card/payment credentials           |
| PUSH         | No sensitive payload; no arbitrary URL/deep-link execution                                 |
| WEBHOOKS     | Raw-body auth/signature, event identity dedupe, replay handling                            |
| FILES/MEDIA  | Content type/size validation, authorization, object ownership, stale upload tokens         |
| DEPENDENCIES | Known-vulnerability scan and review of unnecessary permissions                             |

## 10.1 Authorization abuse tests

- Replace request_id/address_id/context_id with another customer's UUID.

- Replay a CUSTOMER token against RIDER/MANAGER endpoints.

- Attempt to set server-owned fields in request bodies.

- Manipulate status/status-like fields in client payloads.

- Attempt cancel after cutoff, after planning, after completion, and
  against another customer's request.

- Attempt refund-related routes directly from customer where no customer
  command is intended.

- Attempt to register push tokens for another user/session.

# 11. Application leak and local-data verification

The mobile application should be examined as if the device is lost,
shared, debugged or inspected by a technically capable user.

| **Surface**             | **Test**                                                                                                                                        |
|-------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| Packaged app/APK        | Search strings/resources for API secrets, private keys, backend credentials, OTP provider secrets, Razorpay secret, unrestricted GCP server key |
| Secure storage          | Refresh token only in OS secure store; no duplication in plain files                                                                            |
| Memory                  | Access token expected in process memory only; verify it is not persisted on background/termination                                              |
| AsyncStorage/files      | No phone, OTP, refresh token, address/coordinates, payment secrets unless explicitly approved                                                   |
| Logs                    | Inspect logcat/console/crash output for tokens, Authorization headers, request bodies, coordinates                                              |
| Screenshots/recent-apps | Assess sensitive-screen snapshot exposure; mask only where risk warrants                                                                        |
| Clipboard               | OTP/token/address must not be copied automatically                                                                                              |
| Notifications           | Lock-screen payload contains no sensitive address/payment/PII                                                                                   |
| Image cache             | Remote catalogue images acceptable; customer evidence media must obey private-media policy                                                      |
| Backups                 | Check Android backup/exported data settings; secure data should not be restorable insecurely                                                    |
| Crash reporting         | If added later, redact PII/tokens and test breadcrumbs                                                                                          |

## 11.1 Practical leak procedure

1\. Install a release-like nonprod build, authenticate, create an
address and complete representative flows.

2\. Inspect app sandbox/files on emulator/test device where permitted.

3\. Search filesystem and logs for known marker values: test phone,
token fragment, address text, latitude/longitude, OTP, Authorization
header.

4\. Terminate/relaunch and repeat the search.

5\. Logout and verify secure credential/customer cache removal.

6\. Run MobSF static scan on the same artifact and review permissions,
exported components and embedded secrets.

# 12. Network and transport verification

Network testing must verify both confidentiality and behavior under
unreliable connectivity. A proxy such as Burp Suite or mitmproxy should
be used in nonprod.

| **Condition**            | **Expected behavior**                                                            |
|--------------------------|----------------------------------------------------------------------------------|
| Normal HTTPS             | Inspect host/path/headers/body; confirm expected API only                        |
| TLS                      | No HTTP fallback; certificate validation enforced                                |
| Authorization            | Bearer token only where required; never included on unrelated remote image hosts |
| Timeout                  | Slow server beyond client timeout → recoverable error; no infinite spinner       |
| Drop before request      | No server mutation; customer can retry                                           |
| Drop after request sent  | Same prepared command/idempotency semantics preserve one business effect         |
| Drop after response lost | Retry obtains established result; no duplicate booking/cancellation              |
| 401 mid-command          | Single refresh then exactly one replay                                           |
| 403                      | No refresh loop                                                                  |
| Offline startup          | Clear offline state/retry; no destructive session loss from mere network absence |
| Network switch           | Wi-Fi→mobile / reconnect does not corrupt draft/session                          |
| Malformed response       | Protocol error, not misleading success                                           |
| Large/slow image         | Placeholder/caching; screen remains responsive                                   |
| Proxy tampering          | Server rejects changed auth/body/signature-sensitive operations                  |

## 12.1 API traffic inspection checklist

- No phone/OTP/token in URL query strings.

- No Authorization header sent to Blob/public image host.

- No exact coordinates in analytics/log endpoints.

- Idempotency keys contain no PII.

- Cache headers do not make private customer responses publicly
  cacheable.

- Error responses contain no stack traces or raw provider responses.

- Push/device token registration uses authenticated user/session
  semantics.

# 13. Performance and optimization verification

| **Area**         | **Verification**                                                                    |
|------------------|-------------------------------------------------------------------------------------|
| App cold start   | Measure development/release-like start; no excessive synchronous work               |
| Login→Home       | UI usable promptly after auth/principal fetch                                       |
| Home lists       | Smooth vertical + horizontal scrolling; no visible jank on mid-range Android        |
| Remote images    | Correct dimensions, cache, lazy load, no memory spikes/layout jumps                 |
| Address map      | Pin drag/current location/search remains responsive                                 |
| Review           | Inline add/remove does not rerender entire screen excessively                       |
| Activity/history | Pagination/infinite list handles hundreds of historical records                     |
| API latency      | Track p50/p95/p99 for key reads/mutations under representative load                 |
| DB               | Review slow queries/index usage for history, planning work-unit reads, outbox/inbox |
| Workers          | Backlog drains within target; no duplicate effects under concurrency                |
| Memory           | Long navigation session does not show unbounded growth                              |

## 13.1 Suggested initial performance budgets

These are starting engineering targets for nonprod profiling, not
contractual SLAs; revise after measurement.

| **Metric**                | **Initial target**                                           |
|---------------------------|--------------------------------------------------------------|
| Normal API read p95       | \< 500 ms excluding external-provider delay                  |
| Normal API mutation p95   | \< 750 ms excluding provider workflow                        |
| Home interaction          | No obvious frame drops during normal scroll                  |
| History first page        | Perceived usable within ~2 s on normal nonprod network       |
| Image placeholder→visible | Progressive; must not block interaction                      |
| Memory                    | No monotonic leak across 30-minute scripted navigation cycle |

# 14. Resilience, recovery and failure injection

| **Failure**                | **Expected recovery**                                             |
|----------------------------|-------------------------------------------------------------------|
| DB transient unavailable   | API 503/controlled failure; no partial business effect            |
| Service Bus unavailable    | Outbox preserves intent; API transaction remains coherent         |
| Geocoding provider timeout | Technical failure/retry; never false serviceability               |
| Razorpay timeout           | Uncertain state/recovery; never create blind duplicate charges    |
| Refund provider timeout    | INITIATION_UNCERTAIN/reconciliation path; no over-refund          |
| OTP provider timeout       | Challenge/start fails safely; no fake session                     |
| Worker crash               | Replay produces no duplicate business effect                      |
| App killed during payment  | Restart → backend authoritative payment read                      |
| App killed during cancel   | Restart → detail read resolves truth; no client-side compensation |
| Expired serviceability     | Booking refuses stale context and re-resolves                     |
| Expired slot               | Backend rejects; app refreshes slots                              |

# 15. Mobile-native/device verification

Browser preview is useful for visual review but cannot certify native
behavior.

| **Capability**  | **Android**                                 | **iOS**                                           |
|-----------------|---------------------------------------------|---------------------------------------------------|
| Secure storage  | Real device/emulator SecureStore validation | Real device/simulator Keychain validation         |
| Maps            | Google Maps native rendering + permissions  | Apple/Google provider decision + native rendering |
| Push            | FCM token/notification/tap/background       | APNs token/notification/tap/background            |
| Keyboard        | OTP, forms, bottom sheets, review screen    | Same                                              |
| Safe areas      | Punch-hole/notch/navigation bar             | Notch/Dynamic Island/home indicator               |
| Deep links      | Cold/warm start                             | Cold/warm start                                   |
| Fonts           | Bundled font shaping incl. Devanagari       | Same                                              |
| Background/kill | Session/draft/recovery                      | Same                                              |
| Network switch  | Wi-Fi/cellular/emulated loss                | Wi-Fi/cellular/network conditioner                |

## 15.1 Device matrix

- One mid-range Android device representative of expected customer base.

- One current Android flagship/reference device.

- One smaller-screen Android configuration.

- One current iPhone and one older supported iPhone where feasible.

- At least one device with large font/accessibility text enabled.

# 16. Accessibility and usability verification

- Screen-reader labels and reading order.

- Dynamic type / large font without clipping.

- Touch targets approximately 44dp minimum.

- Contrast of gold/cream/secondary text.

- Modal/bottom-sheet focus trapping and dismissal.

- Destructive cancellation confirmation clearly differentiated.

- Errors announced and located near affected control.

- Map interaction has non-drag/manual alternative.

- OTP can be entered/pasted/auto-filled without inaccessible box focus
  traps.

- Status is not communicated by color alone.

# 17. Observability and audit evidence

Observability must help testing without violating the privacy rule that
exact coordinates and sensitive identifiers are not logged.

- Every API request should have a correlation/request ID suitable for
  test evidence.

- Business events should include resource IDs and non-sensitive state
  transitions.

- Worker logs should expose message/event IDs and outcome without
  leaking payload secrets.

- Payment/refund logs must never include credentials or raw sensitive
  provider material.

- Test evidence should link mobile scenario ID → API request IDs →
  backend logs → DB assertion.

- Fluent Bit/Blob log delivery should itself be smoke-tested.

# 18. Automation architecture

Recommended regression pipeline:

1\. Deploy or update Azure nonprod.

2\. Run Alembic migration job and verify schema head.

3\. Load deterministic nonprod fixtures.

4\. Run backend unit/integration suite.

5\. Run deployed API contract/smoke suite.

6\. Build/install Customer Mobile development/release-like artifact.

7\. Run Maestro smoke suite.

8\. Run Maestro regression suite against real nonprod APIs.

9\. Run security static scan (MobSF) on artifact.

10\. Run selected proxy/network scenarios.

11\. Collect evidence and publish a single test report.

## 18.1 Maestro suite organization

| **Suite**     | **Examples**                                                             |
|---------------|--------------------------------------------------------------------------|
| smoke         | Login, Home, address, serviceability, one booking path, Activity, logout |
| booking       | Catalogue, item edit, slots, quote, create, payment states               |
| cancellation  | Eligibility, confirmation, race, cancelled detail, refund lifecycle      |
| history       | Pagination, detail, recommendations                                      |
| session       | Refresh, revoked session, relaunch, user switch                          |
| notifications | Foreground/background/cold tap, duplicate                                |
| network       | Offline, timeouts, response loss, reconnect                              |

## 18.2 Role of Jules/AI reviewer

Use Jules or another code-review agent as a secondary test analyst:
review contracts, generate adversarial cases, inspect uncovered
branches, compare implementation with the traceability matrix, and
analyze failure evidence. Do not treat an AI code reviewer as proof that
a native flow actually executed.

# 19. Manual exploratory testing

A focused human pass remains mandatory before release. The tester should
not merely replay scripted cases; the goal is to discover interaction
failures that scripts do not anticipate.

- Rapid repeated taps and back navigation.

- Interruptions: phone call, background/foreground, lock/unlock.

- Rotate device if supported, change text size, dark/light system
  changes where relevant.

- Deny then later grant location/notification permissions.

- Edit draft in unusual sequence: address → items → address again → slot
  → remove all items.

- Navigate away during pending requests and return.

- Kill app during OTP, booking, payment, cancellation and refund
  observation.

- Poor network/high latency while interacting.

- Visually inspect typography, Devanagari shaping, image crop, spacing,
  keyboard overlap, safe areas.

- Attempt confusing but plausible customer behavior and record product
  friction.

# 20. Test execution phases

| **Phase**                        | **Scope**                                                                 |
|----------------------------------|---------------------------------------------------------------------------|
| Phase 0 – Static readiness       | Code review, contracts complete, lint/type/tests, threat review           |
| Phase 1 – Infra/database         | Terraform + migration + Azure connectivity + secrets                      |
| Phase 2 – Backend smoke          | Auth/test provider, addresses, serviceability, catalogue/slots, API reads |
| Phase 3 – Functional mobile E2E  | Customer journeys through nonprod                                         |
| Phase 4 – Async/business races   | Payment, cancellation, refund, compaction, duplicate delivery             |
| Phase 5 – Security/network/leaks | MobSF, proxy, storage/log inspection, authorization abuse                 |
| Phase 6 – Performance/resilience | Latency/load, device performance, failure injection                       |
| Phase 7 – Human exploratory/UAT  | Real devices, UX/accessibility, odd sequences                             |
| Phase 8 – Release rehearsal      | Fresh deploy + migrations + smoke + evidence pack                         |

# 21. Defect severity and release gates

| **Severity** | **Definition**                                                                                   | **Release policy**                                       |
|--------------|--------------------------------------------------------------------------------------------------|----------------------------------------------------------|
| S0 Critical  | Security compromise, financial duplication/loss, cross-customer access, unrecoverable corruption | Immediate stop; no release                               |
| S1 High      | Core booking/auth/payment/cancellation unusable or wrong durable effect                          | No release                                               |
| S2 Medium    | Material UX/function defect with workaround; no integrity/security impact                        | Normally fix before production; explicit waiver required |
| S3 Low       | Cosmetic/minor usability/documentation                                                           | May follow if accepted                                   |

## 21.1 Mandatory release gates

- All S0/S1 defects closed.

- No known cross-user authorization issue.

- No secrets or refresh tokens found in plain app storage/logs/package.

- All production URLs HTTPS; no unintended HTTP fallback.

- Auth, serviceability, booking and authoritative payment flows pass.

- Cancellation race and refund lifecycle pass.

- Compaction planning race tests pass.

- Deployed DB migration head verified.

- Outbox/inbox duplicate delivery tests pass for high-risk workflows.

- Maestro smoke/regression green on target Android; critical iOS flows
  green before iOS release.

- Manual exploratory sign-off completed on real Android and iPhone.

- Security test evidence reviewed.

- Backup/recovery and deployment rollback/forward-fix procedure
  rehearsed or explicitly accepted.

# 22. Evidence pack and sign-off

Each release candidate should produce one immutable evidence bundle
containing:

- Git commit SHAs for frontend/backend/infrastructure.

- Terraform plan/apply output reference.

- Migration job execution and Alembic head.

- Backend unit/integration result.

- Deployed API test result.

- Maestro reports/screenshots/videos for failed cases.

- MobSF report and disposition.

- Network/security test notes and captured sanitized traces.

- Device/build matrix.

- Manual exploratory checklist and defects.

- Performance summary.

- Known limitations/waivers.

- Final release recommendation and sign-offs.

# Appendix A. Master traceability matrix

| **Area** | **Requirement**                              | **Primary verification**               | **Risk**               |
|----------|----------------------------------------------|----------------------------------------|------------------------|
| AUTH     | OTP start/verify, refresh, logout, principal | Pytest + API + Maestro + proxy         | Security               |
| ADDR     | Address CRUD/map/distance warning            | API + Maestro + native                 | Functional/Privacy     |
| SVC      | Geocode→H3→serviceability                    | API + DB + worker + Maestro            | Functional             |
| CAT      | Catalogue/remote assets                      | API + Maestro + network                | Functional/Performance |
| REC      | History-backed recommendation                | API + Maestro                          | Functional             |
| SLOT     | Authoritative availability                   | API + Maestro                          | Functional             |
| BOOK     | Draft/review/create                          | Jest + API + Maestro                   | Functional             |
| PAY      | Attempt/webhook/status                       | Pytest + API + provider test + Maestro | Financial              |
| PLAN     | Compaction/freeze                            | Pytest + DB + scheduled job            | Concurrency            |
| CAN      | Cancellation/cutoff/race                     | Pytest + API + Maestro + DB            | Concurrency/Financial  |
| REF      | Refund lifecycle                             | Pytest + worker + provider + Maestro   | Financial              |
| ACT      | Active/history/detail                        | API + Maestro                          | Functional             |
| NOTIF    | Registration/payload/tap                     | Native + API + Maestro                 | Security/Functional    |
| MEDIA    | Remote images/evidence                       | API + Blob + native                    | Security/Performance   |
| SESSION  | Secure storage/user switch                   | Jest + native + MobSF                  | Security               |
| NET      | Timeout/offline/replay                       | Proxy + Maestro + device               | Resilience             |
| LEAK     | Storage/log/package                          | MobSF + adb + manual                   | Security/Privacy       |

# Appendix B. High-risk adversarial scenarios

1\. Double-tap Confirm & Pay repeatedly while network is slow.

2\. Kill app immediately after POST collection request reaches server
but before response.

3\. Receive payment callback, kill app before backend webhook; relaunch
and observe pending truth.

4\. Send duplicate captured webhooks with same provider event identity.

5\. Cancel at exactly the planning cutoff while compaction job starts.

6\. Replay cancellation with same idempotency key and then with
different payload/identity.

7\. Crash refund worker after provider refund call but before durable
local update.

8\. Deliver RefundRequested twice.

9\. Change saved address version after serviceability context was
generated; attempt booking.

10\. Use customer A token with customer B address/request UUID.

11\. Expire access token during a destructive mutation.

12\. Revoke refresh session while app is backgrounded.

13\. Rotate push token at logout boundary.

14\. Return malformed JSON with 200 from a test proxy.

15\. Serve slow/large remote catalogue image and inspect memory/layout.

16\. Disable network after tapping cancel, then restore and retry.

17\. Simulate DB/Service Bus/provider transient outage independently.

18\. Inspect logcat while running all flows for Authorization/header/PII
leakage.

# Appendix C. Suggested nonprod test identities and datasets

| **Fixture**                | **Purpose**                                               |
|----------------------------|-----------------------------------------------------------|
| customer_new               | No addresses/history; first-run flow                      |
| customer_history           | Multiple completed collections; recommendation generation |
| customer_cancel            | ACCEPTED, comfortably before cutoff                       |
| customer_cutoff            | ACCEPTED near/past cutoff for race tests                  |
| customer_refund_pending    | Cancelled with pending/processing refund                  |
| customer_refund_done       | Cancelled with successful refund                          |
| customer_multi_device      | Push/session behavior across two devices                  |
| address_serviceable_1      | Known geocodable Delhi address in serviceable cell        |
| address_serviceable_nearby | Same/nearby H3 area for compaction                        |
| address_other_cell         | Different H3 cell for partition boundary                  |
| address_unserviceable      | Known unserviceable result                                |
| address_far_warning        | Selected address far from current device position         |

# Final release principle

| **Definition of confidence.** Tirodhan is ready for production only when repeated automation proves the durable business effects, security testing finds no unacceptable exposure, network/failure tests demonstrate recovery without duplicate financial or planning effects, and a human exploratory pass confirms the native product behaves correctly on real devices. |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|