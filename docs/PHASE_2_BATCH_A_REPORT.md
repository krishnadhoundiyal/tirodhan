# Phase 2 / Batch A implementation report

Scheduling follow-up: the approved Delhi launch availability policy is implemented in
[the operational slot correction](PHASE_2_SLOT_AVAILABILITY_REPORT.md). The original
Batch A results below describe commit `2634ecb` before that approval and correction.

Implementation date: 2026-10-09. This branch is for local review only. Batch A's
production definition of done is **blocked by unapproved operational scheduling
policy**. No operational availability, taxonomy, pricing, receiving-point identity,
approval claim, or product artwork has been invented or seeded.

## Workspace and delivery

Backend: `C:\Users\91956\Tirodhan\tirodhan` (writable).
Implementation branch: `phase2/customer-mobile-core-apis`.
Base: `afa140012db29ec0e83c25f3ff378765a7fe7b88` on clean local `main`, matching
the local `origin/main` reference. The latest inspected main commit is merge #46,
"docs/end-to-end-verification-strategy". The worktree was neither dirty nor
diverged. Existing phase1 branches were inspected and left untouched. Existing
migrations ran through `0017_fleet_dispatch_notification`.

Frontend: `C:\Users\91956\Tirodhan-frontend\tirodhan-frontend` (read only), clean
`main` at `103d181eae4ebc539f8253cb009ac1b42c953e12`. Its required API contracts,
client, DTOs, error codes, capability gates, query hooks and booking caller were
read. No frontend changes, branches or rewriting tools were used.

The final local commit SHA and post-commit Git status are provided in the delivery
message; embedding the report's own commit SHA would be self-referential.

No push, PR, merge, deployment, Azure migration, provider transaction or
infrastructure provisioning was performed. The only database used for tests is
the separate local Docker PostGIS database `tirodhan_batch_a_test` on localhost.

## API and contract matrix

All six GET endpoints are side-effect-free and use the exact frontend paths.

| Method/path | Contract result | Authority/limits |
| --- | --- | --- |
| `GET /v1/auth/me` | `PrincipalDto`; live active roles, no CUSTOMER prerequisite | Existing access-token codec, refresh session and user/role records. Disabled users remain 401 per existing authentication; proposed frontend text says 403. No auth invariant was changed. |
| `GET /v1/customer/collection-catalogue` | `CatalogueDto`, required media properties and artwork nullability | Active persisted groups/categories, deterministic ordering, quick metadata, input requirements and handling hints. Empty without approved data. A category lacking either mandatory image is omitted. |
| `GET /v1/customer/pickup-slots?serviceability_context_id=...` | `SlotsDto` with context expiry, deterministic IDs, UTC start/end, labels, AVAILABLE/FULL | Production returns 503 until an approved availability implementation exists. Synthetic availability in tests is never a runtime default. |
| `GET /v1/customer/collection-requests?view=active\|history&limit=...&cursor=...` | `Page<CollectionSummary>` with required properties | Owned keyset query; required view, default 20, range 1..50. Address summary is empty because no safe historical locality field exists. Nullable image remains null. |
| `GET /v1/customer/collection-requests/{request_id}` | `CollectionDetail` with embedded payment, refunds and journey | Booking address/quote/items and financial/lifecycle persistence; ownership-safe 404. Legacy item display names fall back to recorded category code. Address label is null because no immutable label snapshot exists. Receiving point remains null because historical display metadata is absent. |
| `GET /v1/customer/recommendations/collection-categories` | `RecommendationDto`, at most 12, PREVIOUS_COLLECTION | Own completed collections, active categories/groups, mandatory media metadata. Frequency by distinct request, then most recent collection, then code; rank starts at 1. |
| Existing `POST /v1/collection-requests` | Existing input/success response retained | New independent slot validation, transactional availability/freeze checks and disabled-category rejection. No slot_id requirement. New safe eligibility errors use the proposed envelope. |

No separate journey, payment or refund route was added: this batch implements the
embedded projections. Checkout, atomic cancellation compensation, push,
notifications, profile, preferences, favourites, legal/support content, feedback
and payment methods remain outside this scope. Frontend `cancellationCompensation`
must remain disabled. No frontend capabilities were enabled.

## Persistence and migrations

Reused authoritative models/tables:

- `AppUser/app_user`, `RefreshSession/refresh_session`, `UserRole/user_role`;
- `ServiceabilityContext/serviceability_context`;
- `CollectionRequest/collection_request`, `CollectionRequestItem/collection_request_item`;
- `Payment/payment`, `PaymentAttempt/payment_attempt`, `PaymentProviderEvent/payment_provider_event`, `Refund/refund`;
- `PlanningBatch/planning_batch`, `PickupExecution/pickup_execution`;
- `HandoverEvent/handover_event`, `HandoverEventItem/handover_event_item`.

`0018_customer_mobile_core`, following 0017, adds four narrowly scoped relational
presentation tables: `catalogue_group`, `catalogue_category`, `catalogue_media`,
`catalogue_artwork`. All entity IDs use application UUIDv7, with real FKs,
code uniqueness and VARCHAR/CHECK input/artwork constraints. Media records contain
only approved product Blob keys and presentation metadata. There is no taxonomy
seed, price rule, personal evidence URL, arbitrary public URL or binary media.

The revision adds nullable `collection_request_item.display_name_snapshot` for
new booking-time category labels. Historical records are not backfilled from
mutable catalogue data. It adds indexes for customer ordering, request items,
payment attempts, refunds and pickup-to-handover queries. No existing transaction
is deleted or reinterpreted on upgrade. Downgrade removes only the new tables,
column and indexes. The automated local migration test exercises 0018 -> 0017 ->
0018 and verifies empty catalogue, nullable snapshot and query index.

This revision later needs execution through the approved ACA migration job.
Neither Azure nonprod nor production has been migrated.

## Scheduling and booking

`modules/collection_requests/scheduling.py` owns one Asia/Kolkata calculation.
`daily_grid` generates exactly 48 consecutive 30-minute intervals per local date,
aligned to :00/:30, with correct UTC conversion and next-day midnight rollover.
The deterministic selection ID is `kolkata-YYYYMMDDTHHMM`. Frontend clients receive
only backend-offered AVAILABLE/FULL intervals; they must not generate dates or
windows or infer availability from this grid.

Slot reads authorize the CUSTOMER and query only the owned persisted context.
Expired contexts return 409 SERVICEABILITY_EXPIRED; unserviceable contexts return
409 SERVICEABILITY_UNSERVICEABLE; pending/invalid contexts are ineligible, and a
persisted technical failure returns 503. Missing/foreign context returns 404.
The persisted context supplies cell/location; no client coordinates/cell are used.
Past slots, reached planning cutoffs and already-frozen work units are excluded.
Frozen batches are loaded in one query for the offered grid. Response expiry is
the context expiry, checked again after evaluation. No maximum product horizon,
operating hours, notice rule, service dates or rider capacity was invented.

`SlotAvailabilityPort` requires approved service dates and DB-backed per-cell
availability. `UnconfiguredSlotAvailability` fails closed. The 366-date response
bound is a technical guard that rejects oversized policy output, not a silently
applied booking horizon. The existing planning lead-time setting remains required;
the approximate four-minute lead mentioned in the request was not turned into a
new production default.

Fresh collection creation verifies timezone-aware timestamps, exact duration,
local alignment, future eligibility and owned serviceable unexpired context.
Existing synchronous resolution of a still-PENDING context is preserved.
Creation claims the existing customer/client-request command key, takes the same
cell/start/end PostgreSQL advisory lock used by planning freeze, and rereads date,
cutoff, frozen batch and authoritative availability before inserting the request.
Time, context expiry and payment lifetime are checked again after a lock wait.
Known catalogue categories/groups are locked in deterministic order, disabled
entries rejected and display names snapshotted. The established pricing port
remains the item/pricing authority; unknown historical/test codes are not silently
promoted into the catalogue. Production pricing remains unconfigured without
approved business data.

Request, item, logical payment and idempotency completion commit together.
Same customer/client_request_id plus same fingerprint replays the durable result
before time, category or availability changes; different fingerprint conflicts.
Failed transactions roll back their reservation and are retryable. A lost response
after commit does not create another request, payment or quote. There is no new
external provider side effect. Compaction and singleton fallback are unchanged.

**Scheduling decisions still required:** approved operational service dates,
offering horizon, capacity/availability source and concurrency rules for changes
to that source; any applicable operating hours, additional notice/cutoff or date
restrictions. No rule should be added merely to make a slot look bookable.
Fresh production bookings and slot offering stay unavailable until that approved
source is implemented. Completing the independent APIs does not satisfy this
blocked part of the production definition of done.

## Read consistency, pagination and security

Customer namespaces use the existing live database CUSTOMER role dependency.
Principal discovery uses the authenticated principal dependency without a role
restriction. Every collection/context query applies the authenticated owner;
foreign resources are indistinguishable from missing ones. No JWT role claims,
phone numbers, refresh tokens, precise GPS, H3 cells, payment secrets or provider
charge references are returned in the new customer projections.

Collection pages use created_at DESC/request_id DESC keysets, limit+1 and a fixed
first-page created_at boundary. Cursors are HMAC-SHA256 signed, owner/view/version
bound, size validated and carry a fixed first-page expiry. They contain identifiers
and times only. Subsequent requests cannot extend expiry. Invalid/tampered/foreign
or wrong-view cursors return 422; expiry returns 409 CURSOR_EXPIRED. Stable ordering
excludes ordinary inserts after the boundary. Each page reads current lifecycle
state; a request moving between active/history requires refresh rather than a
long-lived database snapshot spanning HTTP requests.

Configure independent random `TIRODHAN_CUSTOMER_CURSOR_SIGNING_KEY` (at least 32
random ASCII bytes, consistent across replicas) and explicit
`TIRODHAN_CUSTOMER_CURSOR_TTL_SECONDS`. Neither has a production default; absent
configuration returns 503. Rotation invalidates old cursor signatures. Store the
signing secret through existing runtime secret injection, never source code.

Bounded collection selection and batch queries load related item/finance/pickup/
handover facts without per-request SQL. One read-only REPEATABLE READ transaction
provides a coherent projection for each response. Reads neither lock operational
rows nor mutate lifecycle state. Catalogue performs bounded metadata reads and
closes its DB transaction before Blob authorization. No additional cache/broker/
infrastructure is introduced.

Catalogue version is a deterministic SHA-256 of returned approved persistence
metadata, independent of refreshing short-lived SAS URLs. New reads use private,
no-store responses, within the contract's maximum 300-second cache allowance;
the frontend's existing freshness handling must respect signed asset expiry.
Product Blob authorization grants HTTPS/read-only permission to `product-art/`
keys and rejects evidence keys/traversal. Artwork lives separately from household
evidence. Missing runtime fails safely. No live Azure calls were used in tests.

## Financial and lifecycle projection limits

Logical payment success wins later failed attempts/reconciliation. Pending,
processing, failed, confirming, cancelled and expired remain distinct read states.
Retry requires a pending unexpired obligation/request, no reconciliation and no
unresolved attempts; provider/client return never creates success. Missing logical
payment or a success lacking its durable timestamp produces a safe 503.

Refund mapping is PENDING -> INITIATED, PROCESSING/SUBMITTED -> PROCESSING,
SUCCEEDED -> COMPLETED, INITIATION_UNCERTAIN -> CONFIRMING, FAILED -> FAILED.
Completion requires its stored timestamp. Cancellation does not synthesize a
refund or mark one complete. Existing free-form refund reasons outside the three
frontend reasons lack an approved mapping and fail safely with 503; in particular
OPERATIONS_ADJUSTMENT is not silently renamed PAYMENT_CORRECTION.

Cancellation eligibility uses status, configured cutoff, request planning link
and work-unit freeze. It is advisory; the existing POST cancellation remains
authoritative and unchanged. Paid requests use REVIEW_REQUIRED refund expectation
because the commercial compensation policy is open, despite FULL_PAYMENT in the
frontend's example. Missing cutoff configuration makes eligibility false.

Journey BOOKED uses accepted_at, COLLECTED uses a collected PickupExecution,
RECEIVED uses a validated handover's occurred_at and HANDOVER_VALIDATED uses its
evaluated_at. Rejected handovers are recorded without proving receipt. No rider,
receipt, validation or downstream disposal is manufactured. Existing receiving
points lack historical display-name/address/authority snapshots, so null is
returned even when the handover is validated; current master data is not used
to rewrite that historical fact. Legacy item images and summary images remain
nullable null. Safe locality and historical receiving-point display persistence
require a later approved change, not an address-parsing heuristic.

## Verification

Validation results:

| Check | Actual result |
| --- | --- |
| Complete existing/new regression suite, `pytest -q --tb=short`, disposable PostgreSQL enabled | 760 passed, 1 skipped in 498.38 seconds. Covers auth/OTP, address/serviceability, booking, payment/refund/cancellation, planning/compaction, dispatch, pickup, handover/evidence/completion and unit/deployment checks. |
| Final focused unit/API/migration plus changed booking/serviceability regressions | 87 passed in 59.85 seconds: 33 projection/media unit tests and 54 real PostgreSQL integration cases. |
| Final Customer Mobile API and migration checks, including the subsequent freeze-timestamp correction | 17 passed in 24.84 seconds. |
| `ruff check .` | Passed. |
| `ruff format --check .` | Passed; 258 Python files already formatted. |
| `mypy` | Passed; no issues in 136 source files. |
| `git diff --check` | Passed. |
| Migration validation | Local clean-database upgrade through all revisions succeeded; automated 0018 downgrade/upgrade roundtrip passed. `alembic current` confirms 0018 head. |
| Frontend/infrastructure verification | Frontend branch/HEAD unchanged and worktree clean; no diff in infra, deploy, workflows, compose or Dockerfile. |

The complete regression suite ran before the final coverage additions and the
freeze-timestamp correction; the final focused runs cover those changes. Results
are separate runs, not cumulative unique-test totals. The one skipped test is the
existing POSIX signal-exit check on Windows (`test_deployment_job.py:99`), not a
database integration skip. Four existing Firebase SDK deprecation warnings were
reported. No local database verification was omitted. Cloud migration/deployment,
live-provider integration, mobile-native validation and operational availability
verification were deliberately not performed, consistent with this task's scope
and the missing scheduling policy. These results do not establish production
readiness.

Windows sandbox startup failed before execution; read/build/test commands used
approved unsandboxed local execution. Source edits stayed within the authorized
backend repository.

No tests call live OTP, Google, Razorpay, Azure Blob or Service Bus. Provider
adapter tests use explicit doubles; DB integration results use actual PostgreSQL
17/PostGIS 3.5. Windows requires the added `tzdata` dependency for IANA timezone
lookup. This library adds no recurring infrastructure cost.

## Exact changed-file inventory

```text
.env.example
docs/PHASE_2_BATCH_A_REPORT.md
migrations/env.py
migrations/versions/0018_customer_mobile_core.py
pyproject.toml
src/tirodhan/api/router.py
src/tirodhan/api/routes/auth.py
src/tirodhan/api/routes/collection_requests.py
src/tirodhan/api/routes/customer_reads.py
src/tirodhan/core/config.py
src/tirodhan/main.py
src/tirodhan/modules/collection_requests/models.py
src/tirodhan/modules/collection_requests/scheduling.py
src/tirodhan/modules/collection_requests/service.py
src/tirodhan/modules/customer_reads/__init__.py
src/tirodhan/modules/customer_reads/catalogue.py
src/tirodhan/modules/customer_reads/cursor.py
src/tirodhan/modules/customer_reads/errors.py
src/tirodhan/modules/customer_reads/models.py
src/tirodhan/modules/customer_reads/projections.py
src/tirodhan/modules/customer_reads/repository.py
src/tirodhan/modules/customer_reads/schemas.py
src/tirodhan/modules/evidence/azure_media.py
src/tirodhan/modules/handovers/models.py
src/tirodhan/modules/payments/models.py
tests/integration/conftest.py
tests/integration/scheduling_helpers.py
tests/integration/test_collection_payment.py
tests/integration/test_customer_mobile_core.py
tests/integration/test_customer_mobile_migration.py
tests/integration/test_serviceability_runtime.py
tests/unit/evidence/test_azure_media.py
tests/unit/test_customer_read_projections.py
```

No architectural changes beyond the approved Batch A implementation scope were
made. No Terraform, deployment, provider integration selection, compaction,
cancellation/refund compensation, frontend or infrastructure files changed.
