# Geographical operational planning and rolling execution QA

Branch: `codex/operational-geographical-rolling-qa`.
Base: `eefabf5c2174533653f71bdf7e961a039c46b261` (locally fetched merged `origin/main`, PR64).
Local `main` was left unchanged. This commit adds QA instrumentation, fixtures, tests and evidence;
it changes no production module, migration, operational policy or production enablement flag.

## Assessment

The real operational pipeline completed four rolling activations in each primary region and
four in every extended region × threshold × horizon combination. Actual native routing,
CP-SAT selection, independent validation and transactional assignment authority were exercised.
These are operational results, not commercial-admission throughput results or simulated assignments.

The retained evidence comprises **37 scenarios, 160 authoritative ACTIVATED regional cycles and
6,080 real paid/accepted test bookings**. The corrected primary evidence has 320 bookings (80 per
region), 16 activations and 91 final booking assignments. The 36 extended scenarios contribute
5,760 bookings and 144 activations. Final per-region details are below.

Across the retained 160-cycle acceptance dataset there were no recorded historical booking losses,
duplicate incompatible assignments, region-authority overwrites, pickup-window violations or cargo
history rewrites. Every successful cycle incorporated new work. This establishes those properties
for the retained finite inputs and controlled clock, not arbitrary production traffic or global
route optimality. The complete repository suite is **not green**, as detailed below.

## Actual execution and environment

The [execution contract](QA_EXECUTION_CONTRACT.md) was written before the scenario implementation.
The chain is real projected hold → `create_collection_request` → authenticated test capture/replay
→ regional eligibility/wake → `regional_planner.run_region` → live road matrices → spawned native
VROOM/OR-Tools tasks → CP-SAT whole-route selection → refreshed independent feasibility validation
→ `authority._activate` → relational route revisions, pickup executions and rider assignment items.
No final assignment or accepted payment was inserted directly. Identity, serviceability, receiving
points, declared shifts and public pickup locations are synthetic input facts. No real charge or
customer notification was initiated.

Four concurrent region-scoped invocations use distinct `REGION:<uuid>` owners, vehicle memberships,
token/lease fences and confirmed shifts. Region H3 service cells and public road coordinates are
recorded. Thresholds 20/40/60 are a clearly labelled **QA_THRESHOLD_DRIVER**; production has durable
wake/time triggers, not a booking-count trigger. The driver invokes the actual executor, not a
test-only optimizer. The task does not establish broker delivery/scheduler timing end to end.

Local versions: PostgreSQL 17/PostGIS 3.5; OSRM 6.0.0; VROOM 1.15.0; OR-Tools routing and CP-SAT
9.14.6206; native test Python 3.13.5; Hypothesis 6.168.5; seed 1729. Native image ID:
`sha256:c0422f71d51d3a41363086fc6ecc5957ef67921266b9907d698872d587cc6604`.
The existing Dockerfiles pin native image digests and OR-Tools. The DB is disposable
`postgis/postgis:17-3.5`. Native acceptance is bounded to four CPUs/5 GiB and PostgreSQL to two
CPUs/1 GiB; unrelated existing host containers were not stopped. Elapsed timings include local
contention and fixture/database overhead and are not a production latency SLA.

The exact 37,352,545-byte BBBike NewDelhi PBF is versioned, SHA256
`10aa4a42d362a8435b2825220ed448e8c3c45f2a44ce2ac3b487a44a07b9d54b`, timestamp
`2026-10-16T23:00:00Z`. The pinned OSRM MLD/car profile and its SHA are in
[fixture provenance](../../../tests/routing/data/geographical-README.md). This covers broad
Delhi/NCR, rather than substituting the repository's small central-Delhi fixture. Directed road
costs, original coordinates, graph/profile identities and raw responses are retained. The car
profile is an engineering fixture, not a calibrated loaded-fleet/traffic model.

## Measured operational results

| Primary R4 region | Paid | Assigned | Used / declared riders | Road distance | Travel seconds | Waiting seconds | Fleet cost seconds |
|---|---:|---:|---:|---:|---:|---:|---:|
| West | 80 | 21 | 5 / 10 | 61,534 m | 6,188 | 17,754 | 6,248 |
| North | 80 | 28 | 5 / 15 | 38,586 m | 4,555 | 15,632 | 4,685 |
| South | 80 | 21 | 6 / 20 | 113,685 m | 10,160 | 20,678 | 10,460 |
| East | 80 | 21 | 5 / 25 | 19,980 m | 2,284 | 20,780 | 3,004 |

Used-rider shares are 50%, 33.3%, 30% and 20%; mean assigned bookings per used rider are
4.2, 5.6, 3.5 and 4.2 respectively. Counts include existing assigned work; collected work and
custody remain visible. They are not completed pickup counts.

Actual runtime calls over the retained 160 cycles: **7,956 VROOM, 7,956 OR-Tools route solves,
320 CP-SAT coordinator invocations and 1,282 objective-stage solves**. The primary contributes
768/768/32/132 respectively. The two coordinator calls per cycle reflect initial selection and
publication refresh; routing and coordination are distinct operations. Retained candidate failures:
19 VROOM SOLVER_FAILED, two VROOM NO_FEASIBLE_PROPOSAL and six OR-Tools NO_FEASIBLE_PROPOSAL.
They did not create invalid assignments. Counts come from forwarding runtime observers.

Across all 160 invocation measurements, median elapsed time is **53.891 s**, nearest-rank P95
**81.536 s**, maximum **107.056 s**. This combines the corrected primary rerun and original
extended scenarios, excluding the superseded primary evidence. The initial full campaign's wall
duration was 2h 35m 03s. Timing includes production-path work under local resource contention.
No optimality or percentage saving is claimed by comparing different workloads before/after arrivals.

The primary fleets are West 10, North 15, South 20 and East 25 dedicated riders/vehicles.
Extended fleets are 10/20/25 at thresholds 20/40/60. Each extended region has 1,440 paid bookings
across nine independent continuous four-cycle scenarios. All 36 extended scenarios activated
144/144 cycles; their final assigned counts sum to 611 per region across those independent books.
This sum is not simultaneous fleet capacity.

The appointment book spans twelve half-hour slots from 09:30 the next local day. Confirmed shifts
span eight hours and facility windows nine hours. Consequently paid arrivals outside the current
2/3/4-hour routing horizon remain accepted and unassigned; that is not automatically a capacity
failure. Actual per-booking `PlanningOutcome` reasons and separately labelled derived horizon
exclusions are retained. The routing horizon includes travel and discharge; the distinct optional
pickup horizon was not set. No additional reserve percentage or relaxed constraint was introduced.

Native candidate failures are retained, not counted as successful routes. The coordinator can
select independently valid alternatives or incumbents; it does not convert an invalid candidate
into an assignment. CP-SAT statuses, objective values/bounds and exact submitted model protos
establish results only for the supplied candidate pool and completed lexicographic stages.

### Matrix growth

Extended campaign maximum actual persisted matrix nodes over regions/cycles (directed N²/profile):

| New arrivals/cycle | Horizon seconds | Max N | N² entries |
|---:|---:|---:|---:|
| 20 | 7200 | 39 | 1,521 |
| 20 | 10800 | 53 | 2,809 |
| 20 | 14400 | 67 | 4,489 |
| 40 | 7200 | 77 | 5,929 |
| 40 | 10800 | 103 | 10,609 |
| 40 | 14400 | 129 | 16,641 |
| 60 | 7200 | 106 | 11,236 |
| 60 | 10800 | 146 | 21,316 |
| 60 | 14400 | 186 | 34,596 |

The primary maximum was 54 nodes (2,916 entries); physical progress advances its North clock beyond
the extended scenarios. There was no unbounded growth in this finite campaign. Matrices remain quadratic; these inputs
do not prove unlimited regional scale. Paid arrival count is not matrix-node count: horizon
eligibility, incumbent work, origins and facilities determine actual inputs. The declared regional
discovery bound is 10,000 and optional admitted bound 400; the snapshot policy uses 512 nodes and
128 as its base request setting, with the actual regional context applied by the executor.
Neither a computation budget nor this campaign proves commercial capacity.

## Preservation, overlap and physical execution

The independent cumulative ledger tracks every historical assignment, protected task and cargo
observation across cycles, rather than only comparing adjacent plans. It rejects lost or duplicate
ownership, invalid pickup windows, conflicting active rider/vehicle routes, protected-custodian
changes, completion rollback and cargo-history mutation. Every persisted revision proposal is
decoded and passed through the independent feasibility validator. Quick Hypothesis negative checks
demonstrate that loss, duplicate ownership and cargo rewrite cause actual assertion failures.

The additional offline publication audit joins the **exact recorded selection pool digest** to
the archived problem/proposal, independently validates it and matches its pending/started stop
sequence to the committed relational task order. This protects against presenting an unrelated
successful proposal as evidence for an assignment. Full historical snapshots and original metric
files remain in the ZIPs. Current publication metrics and retained revision model metrics have
explicitly different scopes: unchanged remaining task order can intentionally preserve a revision
that includes completed history (`authority.py`), so summing its original full-route estimate is
not the remaining-plan cost.

Primary North executes protected departure, pickups, cargo-bearing replanning and evidenced
discharge through real commands and exact command replay. The separate lifecycle test additionally
queries `PickupAttempt`, `CargoVehicleObservation`, `RouteDischargeProof` and `HandoverEvent` after
commit. It proves COLLECTED custodian identity, a VALIDATED handover with evidence capture ID, and
whole-lot load conservation (3,750 → 2,500 g/ml on discharge of one 1,250 g/ml lot). It does not
claim a real-world handover, customer photograph or media upload.

The adjacent-region test races two version-fenced transfers of the same idle vehicle: one commits,
one raises `RouteAuthorityError`, and exactly one capacity owner remains. While the destination's
real native workers run, a second same-region executor cannot acquire the live lease. The source
returns UNCHANGED and the destination ACTIVATED with one authoritative booking assignment.
Actual transferred-rider usage is **zero**: the engine selected the destination's original rider.
Thus this proves ownership/lease arbitration and absence of duplicate capacity in this case, not
use of one shared rider in both regions or loaded cross-region transfer feasibility.

Seeded Hypothesis real-service histories generate public region subsets, arrivals, cancellation,
clock advances, shift confirmation, capacities and horizons. Three sampled histories covered North
unconfirmed/2,000 g/2 h, South confirmed/100,000 g/2 h and East confirmed/100,000 g/3 h; action totals
were three declarations, three initial booking batches, five plans, four cancellations and six
arrival batches. Full history IDs/exact inputs and native traces correlate; the example database
and failure blobs are enabled. Three samples are not exhaustive generated coverage. Small existing
native oracle tests separately generate directed synthetic contract matrices, cargo/ownership and
protected-work variations; those costs are never presented as geographical road measurements.

## Test execution

| Execution | Actual result |
|---|---|
| Initial primary + extended campaign, before recorder correction | 37 passed, 37 warnings, 9,302.70 s |
| Corrected primary evidence-capture rerun | 1 passed, 1 warning, 537.62 s; 16/16 activations and exact offline publication audits |
| Adjacent transfer / live regional lease race | 1 passed, 1 warning, 76.98 s |
| Seeded generated geographical stateful histories | 1 passed, 1 warning, 88.65 s |
| Real provider timeout, invalid snap and native recovery, final rerun | 1 passed, 1 warning, 22.95 s |
| Explicit pickup/cargo/replan/discharge lifecycle | 1 passed, 1 warning, 63.71 s |
| Existing native Hypothesis/exhaustive-oracle tests | 6 passed, 21.30 s |
| Quick generated ledger negative checks | 2 passed, 10.24 s |
| Full default repository suite, disposable PostgreSQL/PostGIS | **1,476 passed, 2 failed, 60 skipped**, 11 warnings, 2,525.87 s |
| Isolated two-failure rerun, unchanged extracted base, Python 3.13 | 1 passed, 1 failed, 27.22 s |
| Same isolated rerun, QA branch, Python 3.13 | 1 passed, 1 failed, 24.72 s |
| Ruff / formatting | All checks passed; 485 files already formatted |
| mypy | No issues in 227 source files |
| Explicit acceptance without required database | Expected setup ERROR, exit 1; actionable dependency message, no silent skip |

An optional Windows Python 3.10 isolated recheck was interrupted before producing a result;
it is not counted as a pass. The complete native-image results above remain the validation record.

The 60 default-suite skips are explicit existing native/performance opt-ins and the geographical
opt-ins, listed in `regression-results.xml`. They are not counted as successful acceptance tests.
The standalone lifecycle file was added after that suite's collection and executed separately.
The expensive extended matrix was also explicitly executed separately. No test result was inferred
from installed libraries, configuration or an empty solver trace.

The exact commands and packaging workflow are in [README.md](README.md). The wrapper uses seed 1729,
real pinned binaries, graph supervision and isolated PostgreSQL/PostGIS, and refuses a nonempty
artifact directory. See JUnit files for independently inspectable results.

### Full-suite failures and warnings

1. `tests/unit/test_twofactor.py::test_timeout_traceback_and_http_logging_hide_sensitive_url`
   fails on both base and QA code under Python 3.13.5. A boolean-only base diagnostic shows the
   matched code occurs in the test call's own source excerpt in the formatted traceback; API key,
   provider session and phone are absent. This is an existing interpreter-sensitive assertion,
   not evidence that this QA work leaked a provider URL. See `BASE_FAILURE_DIAGNOSTIC.json` and
   `base-failure-recheck.xml`. No OTP or provider implementation was changed.
2. `tests/unit/test_financial_provider_boundaries.py::test_http_ack_waits_for_broker_and_exact_replay_keeps_minimal_identity`
   timed out waiting one second for its broker mock in the full run. It passed in isolation on
   both base and QA code. The timeout is workload/timing sensitive; its full-run cause remains
   unresolved. Do not label the whole suite green or claim a proven base defect from that rerun.

Geographical booking cases emit an existing SQLAlchemy cartesian-product warning from the
reservation/booking `ST_Equals` comparison in `admission.bind_reservation`. Both inputs are
filtered by their unique IDs; no capacity/correctness violation was observed. The full suite also
emits existing Firebase multicast deprecations and one Python 3.13 coroutine-cleanup warning.
Warnings were retained, not suppressed to produce a passing summary.

## Findings and limits

- **Configuration envelope defect, separately reproduced:** `FleetPolicy` accepts max_nodes 513,
  but `regional_planner.run_region` calls `snapshot_configuration(now)` before supplying its
  regional owner/context. `ShadowConfiguration` rejects that larger envelope with “large discovery
  requires a regional owner.” The tested 128/512 configuration works. Reproduce from a captured
  fleet policy using `FleetPolicy.model_validate(policy | {"max_nodes": 513}).snapshot_configuration(now)`;
  constructing the snapshot with regional context from the outset is the proposed correction.
  See `QA_CONFIGURATION_FINDING.json`; no production fix was made in this QA branch. This is not
  evidence that the commercial 128-booking ceiling has been restored.
- **Graph eligibility:** source way vertices are not all car-routable within the existing 100 m
  snap guard. Independent preflight excludes eight North and one South vertex, retaining original
  coordinates/OSM IDs and measured snap distances. Selection does not inspect matrix costs or solver
  outcomes. An earlier attempted geographical run activated 9/16 and correctly rejected seven
  matrices on snap distance; no negative distance entries were observed. This is distinct from
  the previously reported OSRM negative-distance issue; this QA does not declare that upstream
  issue fixed. The explicit recovery test retains an accepted booking and pending wake generation
  across timeout and invalid snap, creates no route on failure, then activates after a real valid
  position command without moving the accepted household or relaxing the guard.
- **QA-only defects corrected:** initial all-in-one-slot input correctly reached FULL at 17 and
  was replaced with an explicitly declared six-hour appointment distribution. No domain constraint
  was bypassed. The primary recorder initially selected “latest” cycles by controlled timestamp;
  physical clock advancement allowed a timestamp tie, capturing North R3 measurements for R4.
  The recorder now uses the returned cycle ID, records run ID and compares publication stop order
  with committed tasks. The primary scenario was rerun, and the retained dataset uses that new
  evidence. All 144 extended cycles have independent exact publication lineage audit as well.
- **Operational realism bounds:** fixed engineering service/load/travel provisions, no live traffic,
  moving GPS feed, real customer media, real charging or production workload. Clock control and
  a 3,600-second lease permit finite reproducible scenarios; this does not establish wall-clock
  lease-expiry safety under the measured native runtimes. The 100 kg/100 litre operational vehicle
  capacity and existing 125% manifest provision are declared inputs, not calibrated business facts.
- **Coverage bounds:** deterministic primary/extended booking windows plus small generated histories
  do not exhaust threshold-boundary timing, arbitrary appointment permutations, prolonged outages,
  manager cargo replacement, loaded cross-region transfers or all combinations of generated events.
  Dedicated ownership and custody paths exercised here do not establish those unexecuted behaviours.
  No claim of global optimum or arbitrary-volume scale is made.

## Changed modules and delivery

New QA modules under `tests/integration`: `geographical_fixture`, `geographical_observers`,
`geographical_evidence`, `geographical_execution`, public-node/snap preparation scripts, the HTML
template, and rolling/stateful/overlap/recovery/lifecycle/evidence tests. New local tooling under
`tests/routing`: geographical supervisor, finite PowerShell runner, publication auditor, report
renderer and ZIP packager; versioned broad-Delhi PBF and source/eligible coordinate manifests.
New documentation and review evidence are under `docs/qa/geographical-rolling`.
There are **no schema changes, migrations, production dependencies or recurring infrastructure**.
The exact historical PBF and compressed native evidence increase repository size intentionally;
raw derived graphs and working outputs remain ignored. Archives have verified CRCs and SHA256.

The branch is suitable for independent review of these finite operational results. It is not a
clean full-suite certification or permission to enable production routing. Review the configuration
envelope defect and existing test failures separately before broader deployment decisions.
No PR, merge, deployment or production enablement was performed.
