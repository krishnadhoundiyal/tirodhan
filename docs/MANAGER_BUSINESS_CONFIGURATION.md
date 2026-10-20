# Manager administration, history and validated replacement

Implementation on `codex/manager-business-configuration`, based on main
`7892d64877d918f218e33b29c2b1fad491fbba49`. This document describes implemented
authority boundaries; deployment and routing activation remain separately controlled.
See the [pre-implementation inventory](MANAGER_BUSINESS_CONFIGURATION_INVENTORY.md).

## Business administration

`POST /v1/manager/business/commands` accepts a discriminated, closed command union:
`CATALOGUE_GROUP`, `CATALOGUE_CATEGORY`, `CATALOGUE_ARTWORK`, `CATALOGUE_MEDIA`,
`PICKUP_PRICE`, `RIDER_PROFILE`, `RIDER_ROLE`, `FLEET_CONFIG`, `RECEIVING_POINT`,
`VEHICLE_CONFIG`, `REGION_CONFIG`. Every command has a stable client command UUID,
resource UUID, expected history revision (zero means create) and non-PII reason code.
Updates are complete typed replacements of the editable fields, not arbitrary JSON patches.
Catalogue codes remain stable. No taxonomy or default production charge is seeded.

Vehicle/region creation, shifts, receiving windows/restrictions, fleet membership and
coverage continue through the existing resource, operational and fleet commands.
Those commands now carry transaction-local history attribution. No second resource
or assignment command authority is introduced. Rider provisioning requires an existing
human application identity; only RIDER role grants/revocations are exposed here.

Receiving location uses longitude then latitude, PostGIS SRID 4326. Structural edits
fail when live reservations, accepted regional commitments, active routes or outstanding
manifest/custody destinations require recovery. Vehicle and region edits preserve the
existing capacity-commitment guards. Suspension/revocation cannot erase physical custody.
Planning-input changes continue through existing source invalidation triggers.

## Flat price

The production API resolves an unconfigured pricing marker to `FlatPickupPricing` using
the injected database session factory. An explicitly injected test port is retained.
Exactly one active `PickupPrice` configures an INR integer-paise fee per booking.
No active valid price fails closed. The technical minimum is 100 paise, matching the
[Razorpay Orders INR minimum](https://github.com/razorpay/markdown-docs/blob/master/api/orders/create.md);
this is not a default business price. A partial unique index prevents two active prices.

The first declared line carries the fee, additional lines carry zero. Their sum equals
the one booking charge regardless of item count, quantity or weight. Category/group
activation and configured input modes are checked. A repeatable-read quote records
`FLAT:<price UUID>:<revision>` in the existing immutable booking/item and Payment
snapshots. Completed booking replay precedes quoting. Price changes do not reprice
historical bookings, payments, checkout attempts or refunds.

## Images

`POST /v1/manager/business/product-images/upload` returns a short-lived, blob-scoped,
HTTPS-only **create-only** user-delegation SAS for `product-art/<command UUID>`.
Use one `Put Blob` request; chunked block upload and overwrite are not authorized.
The semantics follow [Azure Put Blob permissions](https://learn.microsoft.com/en-us/rest/api/storageservices/put-blob).
Finalization inspects stored MIME type/size against existing configured photo policy,
then records a new `CatalogueMedia` reference. It does not persist SAS tokens or image bytes.
Previously finalized assets cannot be overwritten by this upload API. Changing an
image creates a new asset/reference; object-key identity is database-immutable.

`GET /v1/manager/business/product-images/{media_id}/read` resolves historical asset
metadata and temporary authorized read access. Storage unavailability is explicit 503;
media credentials are never history identities. Existing private storage, identity,
retention and resource permissions remain authoritative.

## History model and reads

Migration `0033_manager_business_history` creates the flat-price master and **18 concrete
entity revision relations**, each with a real FK to its domain source. The sources are
catalogue groups/categories/media/artwork, price, rider profile, user role, fleet,
membership, fleet/rider coverage, vehicle, shift, receiving point, region, regional vehicle
membership, rider assignment and fleet-control configuration. JSONB holds immutable
before/after audit snapshots, never mutable core business authority. Region revisions
include ordered coverage/receiving/vehicle associations; fleet-control snapshots exclude
worker lease and scheduling churn. Receiving snapshots expose master longitude/latitude,
not customer locations.

Database capture runs in the source mutation transaction and serializes through its
source row. `SET LOCAL` actor/command/reason settings cannot leak through pooled
connections. Backfilled records explicitly identify a pre-migration baseline with
unknown attribution. System/legacy writes remain distinguishable from attributed ones.
Update/delete of revisions is rejected. Historical actors are not fabricated.

Existing immutable windows, restrictions, shift confirmations, breaks, extension
consents/extensions and slot policies are reused. Nullable command/record-time metadata
is added to these existing facts; old values remain unknown, new command defaults use
the transaction's attribution. Event facts are not described as mutable row revisions.

Manager-only APIs:

| Read | Contract |
|---|---|
| `/v1/manager/business/{kind}` | Bounded current master listing |
| `/{kind}/{identity}/versions` | Chronological revision cursor; current revision identified |
| `/{kind}/{identity}/versions/{revision}` | Specific immutable before/after state |
| `/{kind}/{identity}/compare?before=&after=` | Field-level comparison, actors and reasons |
| `/events/{kind}/{identity}` | Existing immutable duty/facility facts with cursor |
| `/events/{kind}/{identity}/{event_id}` | Specific immutable fact |
| `/events/{kind}/{identity}/compare?before=&after=` | Fact comparison, explicitly event semantics |
| `/bookings/{id}/lineage` | Historical assignments, riders/vehicles, releases, replacement and custody overview |
| `/bookings/{id}/events/{kind}` | Independent bounded pages of attempts/replacements/decisions/transfers |
| `/routes/{id}/versions` | Immutable revision/activation/run lineage and ordered schedules |
| `/routes/{id}/compare?before=&after=` | Compare two route versions |

Paths below the first row retain the `/v1/manager/business` prefix. UUIDs identify
domain entities; fleet-control history uses its existing `REGION:<uuid>` or
`COMMITTED_FLEET` scope. Images compare immutable IDs whose metadata/read access can
be resolved separately. `current_task_state` is explicitly current execution state,
not a historical claim about an immutable route schedule. No history rollback API exists.
PII such as phone, customer address and exact customer coordinates is excluded.

## Replacement options and selection

Existing rider `REPLACEMENT_REQUEST` remains supported. Managers may issue
`MANAGER_REPLACEMENT_REQUEST` with booking, reason, expected current route revision
and `include_route`. A whole-route incident uses the existing `FleetException` to group
individual `RiderReplacement` obligations. Manager identity is recorded without
fabricating rider consent. Failed resources remain restricted; STARTED household
service is not interrupted by an ordinary reassignment.

Evaluation reuses the regional snapshot builder, verified directed road cache/matrices,
VROOM **and** OR-Tools, independent validator, facility-concurrency CP-SAT coordinator
and approved economic measurement. All required regional work remains required;
other current owners are pinned in the advisory witness. Holds, pickup/service windows,
actual cargo, shifts, live eligibility, protected prefixes, authorized facilities and
external facility occupancy remain constraints. Unaccepted flexible capacity is excluded.

For collected material the advisory problem explicitly models acquisition at the
failed donor's fresh observed position, using the existing conservative handling time.
This hypothetical witness accounts for travel/load timing; it cannot be activated or
change custody. The ordinary planner later builds a new problem from actual transfer facts.

Each bounded candidate search attempts both engines. A candidate needs a complete
validated continuation; one engine's failure does not invalidate a witness independently
established by the other. No witness, provider/native failure or exhausted wall budget
means **UNKNOWN**, never certified infeasibility. Search limits, engine outcomes,
exclusions and non-exhaustiveness are published. Defaults are a 30-second wall budget,
at most the configured regional vehicles, existing node/solver bounds and an advisory
expiry of `min(300, lease_seconds)` seconds. Cancellation bounds provider/native awaits.

Options rank committed duty first, then the existing complete regional economic rank,
then stable shift UUID. Ordering of a persisted evaluation is immutable and repeatable;
native heuristic runs are not claimed globally optimal or byte-identical across fresh
runs. Cost, schedules, current cargo, existing bookings, route/source versions and
validated incremental impact versus a feasible surviving incumbent are returned.
If that incumbent cannot be proved feasible at the new epoch, the baseline is explicitly
unavailable rather than reporting an invented zero delta.

Migration `0034_replacement_options` adds immutable evaluations/candidates with concrete
FKs plus request/decision/incident/activation references on the existing lifecycle.
No advisory proposal is a `RouteActivation` or operational assignment.
Scope bounds are checked before quadratic matrix acquisition. Exceeding a bound
returns UNKNOWN while retaining every commitment. Refreshing an approved, pending
request preserves its selected operational target until a manager explicitly selects
a fresh option. Publication records both the solved input generation and the resulting
source generation after its own advisory-pointer updates; the concrete evaluation FK
is deferred within that transaction, not removed.

`GET /v2/manager/replacements/{id}` returns current paginated candidate summaries and
pending/activated/custody status. `/evaluations` and `/evaluations/{evaluation_id}` below
that path expose bounded historical provenance without publishing raw problem coordinates.
`POST /v2/manager/replacements/{id}/select` requires selected candidate UUID, expected
request/source/region versions, expected route versions, stable command ID and reason.
The extended legacy decision command retains compatibility with a default best option;
an arbitrary rider ID is never accepted as validation. Old optional-field defaults
preserve command fingerprints already stored before this increment.

Selection checks candidate membership, expiry, live shift/resource authority, source
generations, region and route versions, and elapsed-time feasibility. Changed facility
timing requires a fresh evaluation. Payment-first/resource-before-source locks, row
versions and serializable decisions fence concurrent selections. Road/native calls
occur outside business transactions. A grouped incident is approved atomically using
one jointly validated candidate, with individual decision and custody records.

## Activation and physical custody

Before pickup, approved selection uses existing release/recovery, retains old assignment
items, and constrains the ordinary regional planner to the selected shift. Status is
APPROVED/PENDING until that planner validates current obligations and commits the new
relational assignment, immutable route revision/stops/tasks and activation. Stale or
failed activation leaves recovery pending; approval is never reported as completion.
An approved pre-pickup replacement can be evaluated and selected again if activation
has stalled, provided physical transfer has not started and no new assignment has
acquired the booking. Its earlier release and decision history are retained.

After pickup, approval prepares the existing transfer intent where safe. Every lot
requires donor/receiver evidence or the existing approved witness pathway. Only actual
`CompleteTransfer` changes current custody. The existing one-outstanding-transfer-per-
vehicle-pair guard remains: grouped lots transfer sequentially using
`REPLACEMENT_TRANSFER_PREPARE` for the next approved lot. Completed lots remain transferred
if a later lot fails; the remaining obligations remain explicit. New custody routes
activate only from those committed facts. Each replacement completes only when its
discharge task is activated on the selected custodian's shift, with identifier-only
transactional outbox notification of activation (no push delivery).

Two defects demonstrated by real native integration were corrected using existing
authority: completion now uses the physical cargo-load query rather than requiring a
global fleet planner/horizon; readiness attributes collected work only to the actual
current custodian rather than also quarantining it under its historical pickup owner.
Historical pickup attempts/evidence and assignment attribution remain unchanged.

No additional service, database, Redis, paid routing provider, production flag,
financial policy or frontend screen is introduced. Existing retention and recovery
obligations apply to the new history. Downgrade refuses adopted business history,
fact attribution or replacement choices rather than silently deleting them; export
and an explicit reviewed rollback procedure are required after adoption.

## Verification

Actual commands, results, failures and supported runtime versions are recorded in
`MANAGER_BUSINESS_CONFIGURATION_VALIDATION.md` before delivery. Native acceptance
uses `tests/routing/run-geographical.ps1 -SkipBuild -Selection tests/integration/test_manager_replacement_native.py`.
Its incumbent split is a declared fixture submitted through existing production
validation/activation; replacement evaluation and subsequent regional activation use
real OSRM 6.0.0, VROOM 1.15.0 and OR-Tools 9.14.6206. Bookings use actual holds,
checkout, provider-event acceptance and replay. Nothing is certified by a mocked
`feasible=True` flag.
The collected-cart scenario continues through individual physical transfers,
authoritative discharge-route activation and actual receiving-point handovers.
