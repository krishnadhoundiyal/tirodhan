# V2 routing increment 1: proposal contract and feasibility gate

Implemented on the current-main base `ef953ab2d8f7a8811a885a7e31b7fbcb626b79a1`.
The 12 October 2026 engineering authorization approves OSRM + VROOM + OR-Tools and
committed-capacity dispatch. This increment implements the first boundary recommended
by the completed evaluation: one versioned input/proposal contract and an independent
validator. It adds no execution switch, API, ORM model, migration, solver installation,
external call, broker route or infrastructure resource.

## Why this boundary comes first

`planning/compaction.py` returns V1 groups, not ordered vehicle routes.
`planning/execution.py` projects one immutable cell/slot batch. Existing dispatch
requires IDLE and voluntary acceptance. `pickups/service.py` returns a rider to IDLE
before `handovers/service.py` validates discharge. Consequently neither a V1 group,
fleet membership nor IDLE is evidence of physical capacity or V2 dispatch authority.
There is not yet a production source of physical vehicle capacity, operating shifts,
current position/cargo, service durations or receiving-point hours.

The new package is `tirodhan.modules.planning.routing`. Its frozen dataclasses are
internal projections, not new persistent entities or an untrusted JSON API. PostgreSQL
will remain authoritative for their source facts. Keeping this gate independent of
either solver prevents native success flags, priorities or cost reports from becoming
business authorization. The experimental VROOM protected-prefix failure and wrapped
reported objective are concrete reasons to reconstruct feasibility and metrics.

## Contract

`DYNAMIC_COLLECTION_ROUTING_V1` is version 1 of the **V2 route contract**, independent
of `BOUNDED_GREEDY_DIAMETER_V1`. Unknown versions fail closed. Both engine adapters
implement `RouteOptimizer.propose`; neither adapter is implemented in this increment.
VROOM constructs new-work candidates; OR-Tools handles committed route suffixes with
hard ownership/prefix constraints. All candidates use `validate_proposal`.

- UUID problem and immutable snapshot identities bind proposals to one input revision.
  Policy, graph, vehicle-profile and engine versions identify the interpretation.
  Resource/facility state versions are carried for future PostgreSQL comparison;
  this pure function does not check live database freshness.
- All times are nonnegative integer seconds relative to an aware UTC epoch.
  Accepted pickup windows constrain **service start**, inclusively. Service duration
  may extend beyond the slot, but all visits and final travel must finish within the
  operating shift. The snapshot builder must conservatively project accepted instants
  into integer bounds; this package never changes the booking's slot facts.
- Directed integer time/distance matrices have explicit ordered node IDs and graph/
  profile provenance. Null arcs remain unreachable. No straight-line substitute,
  symmetry assumption or inferred rickshaw profile is permitted. Matrix acquisition
  and conservative rounding of OSRM times are subsequent adapter work.
- Vehicle snapshots specify rider, capacity vector, initial lots, available instant,
  start/end positions, shift and protected next-stop prefix. Duplicate rider or vehicle
  resources are rejected. Committed capacity requires an explicit operating-shift ID;
  fleet affiliation is not an input. Flexible routes remain marked FLEXIBLE: validation
  proves feasibility for an offer, never voluntary acceptance or assignment authority.
- Dimension names declare units such as grams and milliliters. Every lot specifies an
  integer accounting quantity and nonzero per-unit physical load vector. Partial
  discharge subtracts explicit accounting units with the same load composition.
  No default weights, guessed capacities, arbitrary capacity reset or transfer between
  vehicles exists. This version uses one lot per household request and does not split
  its pickup; splitting its discharge into specified quantities is supported.
- Stops carry hard vehicle eligibility, optional immutable ownership binding, duration,
  service-start window and required coverage. Protected stops must be required and
  bound to their vehicle. The builder must mark accepted commitments required; the
  validator cannot determine booking status from IDs. Unserved optional stops must be
  listed explicitly and exactly once. Required omissions reject the whole candidate.
- Discharge stops identify actual authorized receiving points, not generic depots.
  Their nodes must match the receiving-point snapshots. Availability and ordered
  opening windows are required; unloading **completion** must precede closing and the
  lot custody deadline. Initial cargo is bound to exactly one vehicle; future cargo
  enters only at its household pickup. Each route ends with zero remaining cargo and
  includes travel to its specified end node. This conservative initial contract requires
  eventual unload within the supplied shift; it does not implement overnight custody
  or an open-ended horizon that leaves onboard cargo for another planning execution.
- Caller-specified positive node/stop/vehicle/matrix bounds cap the materialized working
  set. They are technical execution limits, not permission to drop accepted requests.
  Partitioning a larger population into bounded scopes must retain full coverage.

`validate_problem` checks referential integrity, versions, numeric units, uniqueness,
deep tuple immutability and input bounds before use. `validate_proposal` checks exact
served/unserved coverage and independently reconstructs each route's earliest feasible
schedule, waiting, lot balances, load trajectory, authorized unloads, return travel and
shift end. It returns immutable assessments with independently summed integer travel
and distance. It consumes no claimed solver ETA, load or objective. It does not select
an economic objective or claim optimality. A future adapter must bound native integer
costs and penalties and independently check any reported objective before comparison.

## Authority, compatibility and recovery

V1 planning, batch snapshots, assignments, acceptance, pickup completion, handover,
financial rights and singleton fallback execute unchanged. No existing batch acquires
this new contract version. No normal worker imports or calls the routing package.
Advance V2 projections must not transition ACCEPTED to PRE_PLANNING or shorten customer
cancellation rights. H3 serviceability remains resolution 7; no cell or slot partition
is a hard constraint of the new route contract.

The gate has no database, network or file side effects. Exact replay reconstructs the
same assessment; concurrent evaluations share no mutable state. A crash requires only
re-evaluation of the same snapshot. Solver timeout/invalid output must later preserve
the incumbent plan and surface outstanding commitments; feasibility failure is neither
booking rejection nor evidence that singleton routing can satisfy missing capacity.

Before any dispatch integration, a separate increment must implement durable snapshot/
proposal identity, command fingerprint/uniqueness, route revision fencing, live rider
authorization/shift checks, pickup ownership and cargo/facility revalidation under a
documented PostgreSQL lock order. Optimization must run outside those locks. State
change, durable result and relevant outbox must commit together; inbox replay cannot
duplicate assignments. A valid assessment from an old snapshot is not a lock, lease,
freshness check or permission to commit. Existing Payment-first financial rules remain
unaffected. No transactional race/crash behavior is claimed as implemented here.

Routing inputs contain opaque nodes and operational IDs, not addresses, phone numbers,
raw coordinates or credentials. Matrices and route timing still deserve operational
access controls. Future OSRM calls receive exact coordinates only where necessary;
those must not enter logs, exception bodies, inbox/outbox or command metadata. Failure
exceptions expose controlled codes only. Durable routing retention remains a decision
for the persistence increment; this package stores nothing.

This gate validates a **planned** discharge, not evidence of physical handover. V2
kiosk execution still needs order/cargo QR or barcode, photographic capture, trustworthy
timestamps and receiving-point identity. A geofence or optimizer visit alone cannot
decrement authoritative custody or complete a household request. Existing V1 evidence
and upload/recovery rules remain intact.

## Validation and limits

Unit tests exercise both engine labels against the same gate, partial unload and en-route
collection, multiple capacity dimensions, service-start semantics, waiting for facilities,
custody deadlines, unreachable/directed travel, return travel, duplicate resources/stops,
mandatory omissions, explicitly unserved work, protected ownership/order, quantity
conservation, unavailable/unauthorized receiving points, version mismatch and invalid
input. Empty scopes require no fictitious rider; insufficient supply for required work
is explicit infeasibility. These are synthetic contract fixtures, not fresh solver
benchmarks or a claim of real-world capacity/profile calibration.

The new tests do not invoke OSRM, VROOM, OR-Tools or PostgreSQL. Full repository tests
against disposable PostgreSQL/PostGIS separately verify V1 compatibility, including
existing migration and payment-locking tests. Results are reported in the delivery.
The dense matrix validation is O(profiles × nodes²); cargo reconstruction is currently
O(stops × lots × dimensions), with additional bounded resource/reference checks.
No city-wide runtime or cloud sizing claim follows from these tests.

Recorded validation on 12 October 2026:

| Check | Actual result |
|---|---|
| Focused routing tests, Windows Python 3.10.5 | 52 passed |
| Full suite, isolated Linux Python 3.12.15 | 1,167 passed, 4 existing FCM SDK deprecation warnings; 409.39 seconds |
| Database for full suite | Disposable PostgreSQL 17.5 / PostGIS 3.5.2, `postgis/postgis:17-3.5`, including TIGER/geocoder/topology extensions |
| Migration and locking coverage | Existing upgrade/downgrade, populated financial-history/schema test and two-session initiation-term regressions passed in the full suite |
| Ruff lint, formatting, mypy | Passed on Windows and Linux; mypy checked 155 source files |
| Diff whitespace validation | Passed |

The populated-history schema test retains its strict extension-ownership filtering and
the two known application-index metadata differences; no new schema drift was reported.
The slow Windows full-suite run was interrupted and replaced by the completed Linux
run above. Dependencies were installed only in the disposable test container; the
application virtual environment and dependency manifest were unchanged. Both disposable
validation containers were stopped/removed after validation. No GitHub CI run or PR
was requested for this increment.

## Next independently reviewable increment

Implement finite OSRM matrix acquisition plus pinned VROOM and OR-Tools proposal adapters,
using these explicit inputs and independent assessments, with real-engine integration
tests and timeout/invalid-result handling in isolated local execution. Keep production
dispatch disabled. Introduce durable vehicle/shift/cargo snapshots and route revisions
before projecting live bookings or enabling authoritative assignment. Do not manufacture
missing values from optional declared weight or from IDLE.

Before production enablement the owner must settle operating lead time, measured capacity/
service-time policy, profile/access approval, position freshness, custody deadlines,
facility hours/compatibility, insufficient-supply escalation thresholds and economic/churn
objectives. The approved committed/flexible authority split and service-start baseline
are already decisions, not questions to reopen. No approval decision blocks this pure
boundary increment. Graph artifact hosting/job sizing requires measured deployment
evidence and separate approval if it adds recurring infrastructure or cost.

## Evaluation provenance

The completed reproducible study is retained outside the repository under
`C:/Users/91956/.codex/visualizations/2026/10/09/01a11e9d-6e3a-7d93-ae3e-f160669e1e8b/routing-v2`:
`REPORT.md`, `README.md`, `METRICS.md`, pinned dependencies and manifest. It audited 122
final outputs: 117 feasible and five expected rejections, plus 13 validator fault
injections. It tested OSRM 6.0.0, VROOM 1.15.0 and OR-Tools 9.14.6206. This implementation
uses the conclusions without copying experimental code or rerunning the study. The
study's voluntary-dispatch recommendation is superseded by the subsequent engineering
authorization's committed-shift authority. Its performance results are not production
service guarantees. The portable evaluation ZIP SHA-256 is
`1e87dc855be2ae9d709dde0aad882e7c6592536b3610a4cea803406d5e35cdd3`.
