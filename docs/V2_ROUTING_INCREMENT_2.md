# V2 routing increment 2: road matrices and native optimization

Base: `201dbe96a86f63d6f76fab7a92a727e5fde813e9` (merged Increment 1).
The implementation keeps `DYNAMIC_COLLECTION_ROUTING_V1` unchanged and adds independent
OSRM acquisition, VROOM 1.15.0 and OR-Tools 9.14.6206 adapters. It adds no endpoint,
database entity, migration, broker message, production runtime registration or Terraform.
Both implement the existing async `RouteOptimizer.propose` contract. The independent
Increment 1 validator remains unchanged and gates every returned proposal.

## Implemented boundary

`routing/osrm.py` accepts explicitly identified geographic nodes and an injected HTTP
client. It requests OSRM Table durations **and** distances, preserving direction and
unreachable pairs. It rounds each nonnegative value upward to integer seconds/meters;
validates shapes, null masks, zero diagonals and maximum snap distance; and rejects
fallback-speed cells, unsafe logging, redirects, invalid responses, excessive decoded
body size and a whole-request timeout. HTTPS is required except for loopback local
tests. Production callers must use a controlled client with retries disabled, no
coordinate-logging hooks, and approved proxy/TLS configuration. The application's
existing logging configuration disables coordinate-bearing httpx/httpcore diagnostics;
the provider fails closed if those diagnostics are enabled. This cannot prevent logging
by arbitrary custom hooks or an OSRM ingress: such logging must remain disabled there.

The caller supplies explicit graph and vehicle-profile versions. Matrix identity hashes
those versions, ordered nodes, coordinates and rounded results. The returned contract
contains node UUIDs and provenance, not coordinates. Versions are operator attestations;
OSRM's response does not authenticate which graph/profile produced it. Graph artifact
hash verification and endpoint/version binding are deployment prerequisites. No guessed
rickshaw profile, paid matrix API, implicit HTTP retry or straight-line fallback exists.

`routing/preparation.py` validates inputs, creates stable dense native IDs, enforces
native integer ranges and maps each profile's directed matrix. A missing/unreachable
arc is represented inside solvers by a duration greater than the entire shift horizon,
which cannot fit any served route; the canonical matrix retains null. Nodes absent from
a particular profile similarly cannot be visited by that vehicle. There is no geometry
approximation. Conservative total-cost bounds prevent VROOM's unsigned 32-bit report
overflow observed in evaluation. Invalid/overflowing scopes fail before native execution.

Protected prefixes are executed analytically against the snapshot before solving. Their
order, eligibility, hard ownership, windows, service time and cargo are checked; vehicle
suffixes start at their resulting node, time and load. Native engines never get permission
to reorder the prefix. The full prefix is reattached and the **original problem** is
validated independently, including return travel. This supports a moving vehicle as an
explicit current-position snapshot with existing onboard cargo; it does not obtain or
invent a live position, freshness lease or physical movement during the solve.

Stop windows are intersected with authorized receiving-point opening windows and cargo
deadlines. Unloading must finish before closing/deadline; pickup appointments still
constrain service start. Capacity includes all supplied physical dimensions and internal
per-lot integer dimensions, enforcing conservation/ownership across unloading. Routes
must finish empty, as required by the current contract. Discharge choices and quantities
are explicit inputs; these adapters do not choose arbitrary partial amounts/facilities.

## Algorithms and supported capabilities

- VROOM uses one thread, exploration level 5 and a bounded search budget, supplied road
  matrices and duration costs. Whole new lots become native pickup-delivery shipments.
  Already onboard cargo becomes vehicle-bound delivery jobs; their total quantity must
  equal residual onboard quantity. Multiple specified discharges of that cargo are
  supported. Skills enforce eligibility/ownership. Priority 100 aids construction but
  is **not** treated as mandatory coverage: the independent gate rejects omissions.
- OR-Tools uses vehicle-specific arc callbacks, time windows with explicit opening gaps,
  physical/per-lot capacity dimensions, exact initial balances and empty ends, hard
  eligibility, whole-lot pickup-delivery pairs and fixed ownership of residual cargo.
  PATH_CHEAPEST_ARC constructs an initial solution; bounded GUIDED_LOCAL_SEARCH improves
  it. Parallel cheapest insertion failed to construct the initial-cargo regression in
  actual testing; the selected construction passes it. Specified split discharge of a
  new pickup is also supported through nonnegative per-lot dimensions and empty ends.

VROOM cannot faithfully express the latter case as one pickup with multiple shipment
deliveries in this projection. It raises `VROOM_SPLIT_NEW_LOT_UNSUPPORTED`, rather than
duplicating a pickup or weakening ownership. Ambiguous alternative/overcomplete onboard
delivery sets raise `VROOM_CARGO_BALANCE_UNSUPPORTED`. Both engines support the common
whole-new-lot plus specified partial-onboard-discharge fixtures. This is a capability
restriction on an adapter, not a restriction or change to the common contract.

The engineering search objective is hard coverage/feasibility first, then optional
coverage and travel duration. OR-Tools uses a bounded optional-stop penalty greater than
any complete travel cost, favoring coverage before duration. VROOM prioritizes job/
shipment coverage with duration as cost. For common whole-lot inputs optional pickups
and deliveries are paired; split optional choices need not have equivalent coverage
weights. Only compare engines on the same supported scope and independently reconstructed
coverage, travel, distance and schedules. Every native reported duration objective is
checked against residual route arcs (plus OR-Tools' explicit unserved penalties); native
ETAs/loads never establish correctness. Neither engine proves global optimality, nor
does a timeout/no solution prove that a business commitment is infeasible.

The objective does not implement an approved economic tradeoff, driver churn price or
automatically confer priority/authority on a rider. Required commitments and supplied
eligibility/ownership are hard constraints. COMMITTED/FLEXIBLE classification is retained;
dispatch policy must first scope committed capacity and explicitly decide when to invite
flexible supplementary capacity. Neither solver's vehicle choice constitutes acceptance.

## Failure, replay and recovery

`routing/execution.py` launches native code with private stdin, no shell, bounded stdout
and discarded stderr. OR-Tools runs in a child Python interpreter, never native execution
in the API event loop. Cancellation, deadline expiry and oversized output kill and reap
the child. Missing/version-mismatched binaries, malformed/duplicate-key JSON, nonzero exit,
infeasible/invalid proposals and mismatched objectives produce controlled error codes
without coordinates, payloads or native stderr. Maximum search defaults to 30 seconds;
default process overhead is 10 seconds. VROOM's finite version probe has its own overhead
deadline before search. Scope bounds cap materialization; local test containers additionally
bound CPU/memory. An eventual finite Job needs an explicit overall budget/concurrency cap.

Inputs remain immutable; each attempt owns its projection/process. Concurrent evaluations
have no shared mutable state. A crash or failed request leaves all operational state
unchanged and the same snapshot can be evaluated again. Heuristic wall-clock search can
return a different feasible ordering on replay, so these adapters do **not** promise
byte-identical proposals. They perform no business mutation or provider side effect and
need no new command/inbox/outbox record in this increment. A future durable run must have
a snapshot/policy/engine/budget fingerprint, unique run identity, recorded winner and
idempotent publish/accept behavior. Solve outside locks; fence route/resource revisions
and revalidate live authority/custody inside the assignment transaction. Preserve incumbent
commitments on failure and surface unsolved required work; never silently replace it with
infeasible singleton routes. Financial Payment-first locking is untouched.

## Compatibility and infrastructure

OR-Tools is an optional `routing` extra pinned to 9.14.6206, absent from default API/dev
installation. VROOM is a separately pinned executable, not a new Python or cloud service
dependency. No production worker imports/calls these adapters. V1 planning, acceptance,
IDLE semantics, stable PickupExecution, handover, financial state, auth and recovery
continue unchanged. V2 proposals alone cannot establish assignments or custody.

The local runner pins VROOM/OSRM image digests and checks a small attributed OSM PBF hash.
It creates bounded containers and one temporary graph volume, with no host port, and
cleans them in `finally`. See [reproduction instructions](../tests/routing/README.md).
Graph binaries are generated, not committed. Test-image Python/system-package and wheel
transitive versions can change on rebuild; engine versions and input artifact are pinned.
The complete test image digest/version output records an actual execution environment.

No recurring infrastructure or cloud cost is authorized or provisioned. Native solvers
fit finite independently scaled execution; OSRM needs a loaded road graph, memory and
startup time even when requests are sparse. Options remain finite local/Job co-location
with graph preparation, a separately scaled ACA container with measured cold-start/load
cost, or a minimum replica with explicit recurring cost approval. No hosting option is
selected here. Graph updates, artifact distribution/storage/egress, TLS/private connectivity,
license compliance and loaded-vehicle access profiles need deployment decisions/evidence.
API co-location and a public demonstration endpoint are not production recommendations.

Dense OSRM responses and adapter matrices cost O(profiles × nodes²). OR-Tools adds one
lot dimension in addition to physical dimensions, so solver memory/search grows materially
with lots, stops and vehicles; VROOM also receives these vectors. Prefix/cargo preparation
and Python callbacks are bounded but not optimized for city-wide throughput. VRP search
is combinatorial; a one-second four-node test establishes integration/feasibility only.
Cross-cell/cross-slot scopes are permitted by the contract and these adapters have no
cell partition; scalable rolling scopes, backlog completeness, objective policy and
production resource sizing remain separate work, using the earlier benchmark as baseline.

## Tests and next increment

Tests include real pinned engines on initial cargo, protected collection prefixes,
specified partial unload, multiple vehicles/profiles and hard ownership, facility opening
gaps, physically oversized required pickups, unreachable optional work, infeasible-solve
recovery and concurrent evaluation. Real OSRM Table output feeds each real solver and
the unchanged independent validator. Mocked HTTP tests cover malformed/unreachable,
rounding, snap limits, versions, timeout, response bounds and retry after failure. Actual
child-process tests prove timeout/cancellation termination/reaping, crash recovery and
controlled output. Injected native success/overflowing objectives cannot bypass the gate.
Full suite and lint/type results are recorded after validation below.

The recommended next increment is durable operating resources and immutable snapshot/
run identity: measured capacity/units, shifts/authority, timestamped positions, cargo
projection from collected/handover facts, facility hours/profiles and route revision
fencing, with snapshot-building tests. Keep authoritative assignment disabled while
shadow runs compare common engine inputs. IDLE cannot imply zero cargo. Only a later
transactional dispatch increment may commit routes under live authorization/ownership
checks, preserving flexible acceptance and incumbent custody. Production graph hosting,
profile calibration, policy thresholds/objectives and recurring cost need owner approval
before enabling runtime operations, but did not block this isolated engineering increment.

## Recorded validation — 12 October 2026

The final reproducible PowerShell runner completed **50 tests passed in 16.73 seconds** on
Linux Python 3.13.5, with real OSRM 6.0.0, VROOM 1.15.0 and OR-Tools 9.14.6206. This
includes 18 opt-in native-engine tests and 32 acquisition/process tests. The test image
for the completed run is
`sha256:523437888cc66267cb4911ed605a568266305ca069f287e9799bfe0686410748`.

| Engine | Solve/process elapsed | Validated travel | Validated distance |
|---|---:|---:|---:|
| VROOM | 0.031 seconds | 557 seconds | 3,359 meters |
| OR-Tools | 1.404 seconds | 557 seconds | 3,359 meters |

These are one-second search-budget runs on four public nodes with synthetic cargo,
appointments and capacities. They include child-process overhead and exclude prior matrix
acquisition; they are not a new city-scale benchmark or a performance guarantee. OSM
graph extraction/partition/customization succeeded with a clipped 407,402-byte source;
OSRM reported 3,715 edge-based nodes / 13,630 edges. Native tests also run directed
synthetic profiles, required and optional work, protected pickups and discharge splits.
The first packaged test run had two test-field AttributeErrors (`service_start` versus
`service_start_at`) in an older build context; the rebuilt run above includes the fix.
No validator or solver correctness relaxation was used. Test servers and graph volumes
were removed on both failure and success.

Windows Python 3.10.5 separately passed the 32 ordinary routing tests. Ruff lint/formatting
and mypy passed on Windows and Linux; mypy checked 160 source files. Linux formatting
checked 323 Python files. Default repository tests explicitly skip the 18 opt-in native
cases; their real execution is the separate successful run above.

The first full default-dependency run completed with 1,198 passed, one failed, 18 skipped
and four existing FCM SDK warnings (405.77 seconds). The failure was test isolation:
earlier Alembic `fileConfig` disabled the httpx logger, whereas `caplog.set_level` alone
did not re-enable it. The test now explicitly enables diagnostics with automatic state
restoration and asserts they are enabled before checking the provider's privacy guard.
No production logging or provider behavior was changed. The full suite was rerun after
this correction; its completed result is recorded below.

The optional extra's resolver selects OR-Tools' required protobuf 6.31.1 together with
compatible Google packages within the pre-existing manifest ranges (in the observed
resolution: google-api-core 2.33.0, google-cloud-firestore 2.27.0,
googleapis-common-protos 1.75.0, grpcio-status 1.80.0, proto-plus 1.28.2). This differs
from the latest default installation. Do not force the solver into an existing API
environment with `--no-deps`; use an isolated native execution image or a fully resolved
optional installation. No default application dependency constraint was changed.

An isolated Python 3.12.15 virtual environment with the fully resolved optional extra
passed `pip check` (no broken requirements), mypy (160 source files), all eight selected
real OR-Tools tests (17.14 seconds; ten other native cases deselected), and the three
notification-worker tests (2.64 seconds). The latter check exercises the SDK-facing worker
with the compatible protobuf/Google dependency set. Default suite execution continued in
its original environment; no dependency was replaced under a running test process.
The eight dispatch-push tests, including real Firebase SDK multicast construction, also
passed in that optional environment (1.06 seconds; four existing SDK deprecation warnings).

The completed full-suite rerun on Linux Python 3.12.15 / disposable PostgreSQL 17.5 /
PostGIS 3.5.2 (`postgis/postgis:17-3.5`, including TIGER/geocoder/topology) passed:
**1,199 passed, 18 opt-in native skips, four existing FCM SDK deprecation warnings in
519.46 seconds**. All skipped cases passed separately in the pinned real-engine run.
The full run includes migration upgrade/downgrade, populated financial-history/schema,
all three initiation-term triggers, application-drift injection tests and the two-session
payment-term locking regressions. The strict Alembic check still permits only the two
known application-index metadata differences (`ix_collection_request_pickup_location_gist`
and `ix_collection_request_planning_batch_id`), excluding extension-owned objects through
catalog ownership. No new schema drift was reported; no schema/model/migration changed.

Ruff lint, formatting and mypy passed again before the completed full rerun. Whitespace
diff checks passed. Disposable validation containers and native graph volumes are removed
after validation; local cached test images/artifacts remain. The increment is committed
for review only: no push, PR, merge, deployment or subsequent increment is performed.
