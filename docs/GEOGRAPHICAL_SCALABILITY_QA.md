# Independent geographical scalability and custody QA

Target: `d9b676d74fd84394789b1909c2afa0335f5b69a9`.
QA branch: `codex/geographical-scalability-qa`.

This branch changes tests, synthetic/public fixtures, measurement tools and QA evidence only.
It changes no application module, migration, production configuration or approved ADR.
Production routing remains disabled. No production service, paid provider, deployment or PR
is introduced. The conclusions below concern the exact target, including observed failures.

## Qualification decision

**Not qualified for production geographical/custody readiness.** The measured extract is
approximately 0.02 degrees by 0.02 degrees, not a representative full Delhi road graph.
Passing fixture campaigns cannot establish city-wide scale, traffic accuracy or physical
suitability of the uncalibrated shipped OSRM car/bicycle profiles.

There are independently reproduced custody/liveness failures and a real invalid OSRM response.
No financial promise was accepted on the basis of an invalid matrix in these tests.
Performance results and bounded campaign completion are recorded below, without interpreting
a time-budget yield as physical infeasibility or a restored commercial booking-count ceiling.

## Reproducible geographical inputs

The dataset is the checked-in, attributed public OSM extract described in
[`tests/routing/data/README.md`](../tests/routing/data/README.md). Its SHA-256 is
`4f36f7e749aef6f21128288ab8ca6447666a506a440d74db778a079974a71b49`.
The original full source is not used to make Delhi-scale claims. No household/rider observations
or customer addresses are present. All orders, shifts, vehicle loads and facilities are synthetic.

[`geographical_fixture.py`](../tests/routing/geographical_fixture.py) reads the pinned standard
PBF representation, including delta-coded dense nodes and way references. It selects public
road vertices with deterministic farthest-first sampling and seed `20261018`. Access restrictions
are not bypassed. OSRM's actual profile controls snapping and directed journeys; geometry is
used only to sample fixture coverage, never to substitute travel distance/time.

[`geographical_preflight.py`](../tests/routing/geographical_preflight.py) checks actual nearest
positions and then the **Table sources**. A first 512-coordinate sample had only 511 Table
positions despite 512 Nearest positions. Replacing the duplicate deterministically establishes
512 distinct Table positions for D. This difference is a QA fixture issue, not evidence of
an incorrect road distance or an application snap-repair mechanism.

The manifest retains original OSM IDs, input and snapped public positions, way references,
input-way one-way tags, bounds, raw table hashes, quantiles, invalid-cell examples and full synthetic
capacity witnesses. Campaign A/B/C/D have 64/128/256/512 distinct coordinates and Table snaps.
Control has 512 bookings at the original 64 sites. E has 512 bookings, 448 distinct sites/snaps,
clustered demand, fringe sites, one-way roads and 64 intentionally repeated physical accesses.
No successful reservation/certificate/accepted order is bulk inserted by the benchmark.

All six workloads have an independently constructed feasible witness with 80 declared future
vehicles, 10 kg / 10 L capacity each, 180-second pickup and 60-second discharge. The simulator
uses raw directed road arcs for a pickup-then-discharge sequence, respects service-start windows,
shift end and dated facility opening, and empties each vehicle before its next pickup.
Household reservations are at most 2.5 kg / 2 L. The witness schedule is retained per input index
and vehicle. It is a feasibility witness, not a route quality/optimality claim.

The actual campaigns retain the original distribution: 70% of bookings in the first two
30-minute slots and the remainder in six later slots. Vehicles start 15 minutes before the
first slot and finish eight hours after its start; the synthetic authorized facility is open
from one hour before to twelve hours after. All campaigns use the same 80 vehicles so resource
count does not explain their different geographical cost. For the original report, only its
512-booking/80-vehicle run is directly comparable in population/resource count; smaller original
runs had fewer vehicles. The original 64-site hull is smaller than A–D's hull, so the control
comparison varies both site diversity and network extent, at fixed bookings and resources.

## Independent correctness boundary

Each successful publication is intercepted by test-only instrumentation. After ordinary
production persistence, the QA check reconstructs the proposal from temporal relational proof
membership with `admission_store.load_certificate`, compares it with the proposed order, and
runs the complete independent validator without shared-proof reuse. A separate integer route
simulator checks required stop coverage/uniqueness, pickup-before-discharge balances, physical
load, directed journeys, windows, shift return and authorized discharge. Live source role/status
facts are also checked. These checks add measured work; their time is explicitly retained.
They do not replace the transaction's source-head/lease/Payment-first guards or change its result.

Final A/B/C/D/E instrumentation additionally compares the problem's obligations with actual
reservation/accepted-booking rows, independently of the production snapshot builder. The control
was separately audited from a consistent SQL read of all relational memberships and coverage
links: all 512 historical revisions preserve every earlier reservation, with 131,328 checked
obligation/revision pairs and zero missing or unexpected pairs. The reusable auditor and exact
capture statement are in `tests/routing/geographical_history.py`.

The small pruning oracle independently enumerates integer departure/service starts, breaks and
all legal pickup/discharge gaps for 20 deterministic directed, nonmetric instances. It calls
neither the insertion calendar helpers nor the feasibility gate. The existing larger independent
gate-comparison regressions are run separately. A valid witness, not search failure, is the
admission criterion.

## Findings

### P1: An expired resource's obligation blocks unrelated commercial admission

The custody reproduction performs actual authorized route activation and two successful physical
pickups, leaving no uncollected original booking. Discharge deadlines are explicitly authorized
before activation to remain valid after expiry, and the facility stays open. Natural time advancement passes the first
shift's end; its terms are never extended to preserve cargo. A second, empty resource has an
independently declared longer shift, fresh position and regional membership.

Observed:

- The expired resource is excluded from available capacity.
- `cargo_load` still reports 8,000 g and the successful pickup history remains `COLLECTED`.
- The orphaned route explicitly remains an unresolved operational obligation.
- The other eligible resource is present, but the new admission remains `PENDING` with
  `REGIONAL_INPUTS_REQUIRE_ATTENTION`.

The mechanism is [`rolling.ready_snapshot`](../src/tirodhan/modules/routing_operations/rolling.py#L333)
reporting `SHIFT_END_REQUIRES_RECOVERY` for an orphan, then
[`admission.reserve_capacity`](../src/tirodhan/modules/routing_operations/admission.py#L256)
rejecting the entire regional readiness set whenever **any**
issue exists. Regional operational planning quarantines issues, but commercial admission does
not distinguish an isolated unresolved obligation from otherwise certifiable independent capacity.
An orphan is therefore safe from silent deletion but can block unrelated regional sales.
This is a liveness/functionality defect, not observed overselling or lost physical custody.

Reproduction: [`test_geographical_custody_qa.py`](../tests/integration/test_geographical_custody_qa.py).
Machine observation: [`GEOGRAPHICAL_QA_CUSTODY.json`](benchmarks/GEOGRAPHICAL_QA_CUSTODY.json).
The qualification assertion deliberately **fails**, rather than xfail or weaken the requirement.

### P1: Loaded expired shifts cannot reach the required next-shift/discharge lifecycle

In the same reproduction, [closing the old shift](../src/tirodhan/modules/routing_operations/commands.py#L411)
returns `SHIFT_HAS_OBLIGATIONS` because material
remains onboard. Opening a new shift for that same vehicle/rider fails the one-open-shift DB
constraint (`uq_open_rider_shift` observed; `uq_open_vehicle_shift` also exists).
The vehicle is therefore not certified as a future empty vehicle, which preserves safety, but
the requested future-shift-with-existing-cargo workflow is not available through these commands.

Trying to start the retained discharge task after natural expiry returns `ACTIVE_SHIFT_REQUIRED`
before discharge validation. The same active-shift check gates both pickup and discharge in
[`execution.execute_task`](../src/tirodhan/modules/routing_operations/execution.py#L183).
The test does not bypass it with an internal handover helper, fabricate
a handover, close a cargo-bearing shift in SQL, or keep the expired shift artificially active.
Consequently the required post-expiry handover/release could not be demonstrated through the
vehicle-bound execution path. Ordinary valid handover/recovery tests are separately exercised.
Authorized transfer remains an existing recovery option; it does not itself demonstrate the
requested same-vehicle next-shift and expired-rider discharge lifecycle.

This needs an explicit, consistent operational transition preserving custody and resource
authority, plus expiry/recovery/admission tests. Production code is unchanged on this QA branch.

### P1: Broad facility-window reads can block a feasible future booking

The controlled with/without comparison uses the same future booking, declared resources,
physical load, dated open receiving point and independent travel fixture. Without an unrelated
opening, admission succeeds. Adding an otherwise valid opening entirely before the booking's
routing epoch leaves admission `PENDING / INVALID_FACILITY_HOURS`, with no new reservation.
The captured opening offsets are `[-7200,-3600]` and `[39600,86400]` seconds. The latter is the
valid future opening; the former should not make it unusable.

`AdmissionReadCache.capture` reads a 48-hour superset. `project_horizon` narrows vehicles,
requests, routes and tasks but retains the broad `windows` list. `snapshot.build_problem`
clamps each start to zero while retaining its negative end, creating `TimeWindow(0,-3600)`.
The independent gate correctly rejects that invalid contract. The defect is projection/liveness,
not the validator or evidence of insufficient physical capacity. No window/provider rule was
relaxed in the QA. Machine comparison: `GEOGRAPHICAL_QA_WINDOW_0.json` and `_1.json`.

The existing `test_bound_paid_booking_reuses_certificate_without_reoptimizing_incumbents` also
raised `INVALID_FACILITY_HOURS` in the first verification run. The controlled reproduction above
establishes the projection defect independently; the first run did not capture that existing
test's individual windows, so its exact trigger is not asserted solely from its error code.

### Existing OSRM release blocker: negative distance at public snapped road positions

The mixed Table has a negative distance despite a directed feasible workload witness.
The example reduces to two public vertices (OSM IDs `12319493474` and `11778255747`): raw
OSRM 6.0.0 returns distance **−0.3 m** in one direction and **+0.3 m** in the other, with
nonnegative durations. The input vertices are about a road-snap displacement apart; their
**snapped** positions are near-coincident. This is not a claim that their input pins coincide.
The exact public inputs, raw response and graph hashes are retained. No customer data is logged.

The normal provider rejects `INVALID_ROAD_MATRIX`; the directed cache retains no invalid cells.
The QA does not clamp, snap-rewrite, call an alternate distance API or fabricate fallback travel.
This reproduces the previously documented provider release blocker with an additional public
counterexample; this assignment does not independently patch or re-diagnose OSRM internals.

### Missing sparse arc and unreachable road remain distinct

A separate pinned synthetic OSM topology has two road loops connected by a one-way bridge,
an access-restricted alternative, and a disconnected third loop. Car travel west→east exists;
east→west is confirmed unreachable. Explicit bicycle permissions permit both directions.
Disconnected-loop travel remains unreachable for both profiles. Raw OSRM distances are used.

Before acquisition, the reverse arc raises `ROAD_ARC_NOT_ACQUIRED`. After acquisition, the
car reverse arc is present with null travel and raises `UNREACHABLE_ARC`. The full gate rejects
the latter as a route leg. Missing-arc injection in the actual durable admission workflow
remains UNKNOWN/PENDING, not a physical-infeasibility claim, and succeeds after cache loss/retry.

### Measured storage/validation amplification

The final 64-site control creates **37,563** immutable route certificates/memberships and
**130,175** proof-obligation links for 512 bookings. This is approximately 73 route certificates
per booking, not one newly persisted route per insertion. The full-validation counter is 39,101
calls, including 512 extra QA full checks. The target already documents conditional sharing:
route fingerprints retain hard terms, including the problem epoch and relative calendars.
Changing the per-intent slot/horizon can invalidate otherwise similar routes. These observations
quantify the conditional-sharing cost; they are not a new schema difference or permission to
remove hard terms from the fingerprint. Performance/storage sizing must account for this cost.

## Measurement interpretation

### Executed final campaigns

All cases use 80 declared vehicles. Site counts below are distinct input coordinates **and**
distinct Table source/destination positions, verified in both directions. Times are seconds.

| Case | Bookings / sites | Admitted / pending | Pipeline time | Admitted/s | Decision P50 / P95 / P99 |
|---|---:|---:|---:|---:|---:|
| Control | 512 / 64 | 512 / 0 | 373.76 | 1.370 | 132.33 / 298.71 / 314.83 |
| A | 64 / 64 | 64 / 0 | 34.57 | 1.851 | 7.77 / 27.03 / 28.46 |
| B | 128 / 128 | 128 / 0 | 78.98 | 1.621 | 22.61 / 62.12 / 65.71 |
| C | 256 / 256 | 256 / 0 | 207.43 | 1.234 | 65.60 / 172.32 / 182.50 |
| D | 512 / 512 | 512 / 0 | 627.57 | 0.816 | 187.60 / 539.66 / 574.33 |
| E | 512 / 448 | 250 / 262 | 207.52 | 1.205 | 70.55 / 172.31 / 182.04 (completed subset) |

Control/A/B/C/D complete. E exhausts its **180-second worker budget**; its 262 remaining intents
are still PENDING and unattempted, with zero terminal rejections, explicit UNKNOWN failures,
expirations or retries in that campaign. Its percentiles are censored and cannot be interpreted
as full-population latency. Other cases use a 600-second worker budget. Pipeline times additionally
include enqueue and checkout/capture, so D's 627.57 seconds does not mean its worker exceeded
600 seconds. An earlier D run admitted 485 and left 27 unattempted at that same worker budget;
it is retained as exploratory evidence. Completion is sensitive to runtime conditions near this
budget; no throughput SLA or reliable full-burst drain bound follows from a single successful run.

The E negative-pair inputs occur at booking indices 248 and 400. Its bounded run reaches only
the former; **no real provider failure occurred in that macro campaign**. A separate two-booking
real-OSRM admission regression accepts/pays the first booking, leaves the second durably pending
with `INVALID_ROAD_MATRIX`, loses process caches, retries, and preserves the first acceptance.
It records two failed attempts, one valid reservation and zero false promises. The permanent bad
matrix does not magically recover; clean-road recovery and provider timeout/backoff are tested
separately. See `GEOGRAPHICAL_QA_DURABLE_NEGATIVE.json`.

| Case | OSRM queries | Returned cells | OSRM CPU s | Application CPU s | DB CPU s | SQL calls / cursor seconds |
|---|---:|---:|---:|---:|---:|---:|
| Control | 103 | 59,941 | 0.96 | 299.94 | 66.97 | 223,197 / 202.95 |
| A | 103 | 59,953 | 1.13 | 22.89 | 5.22 | 16,965 / 20.91 |
| B | 337 | 255,113 | 3.79 | 51.65 | 11.66 | 46,207 / 41.13 |
| C | 1,200 | 1,051,762 | 14.50 | 129.75 | 27.14 | 107,079 / 103.75 |
| D | 4,508 | 4,269,570 | 60.46 | 369.41 | 79.42 | 233,703 / 330.04 |
| E (partial) | 1,068 | 924,422 | 13.26 | 133.27 | 29.17 | 106,572 / 108.23 |

At equal 512 bookings/resources, D takes **1.68×** the control's pipeline time, makes **43.77×**
as many matrix queries and returns **71.23×** as many cells. Increasing population at 64 reused
sites substantially understated road-acquisition cost. The per-profile retained arc charge reaches
2,000,000 bytes at B and stays there (the other half of its 4 MB budget covers snapping metadata),
while acquisitions continue growing. No cache/solver memory budget or validation constraint is
weakened. This is an observed fixture-scale cost increase, not a proof of asymptotic production
throughput or a reason to infer full Delhi hosting requirements from the tiny graph.

All final macro certificates pass the fresh full gate and separate route simulator: **1,722**
certificates in total. All successful reservations remain visible and become actual paid/accepted
bookings through the service workflow. Native fallback calls are zero in these feasible insertion
campaigns, for both engines; their native correctness is exercised by the separate pinned suite.
No deadlock, temporary-file spill or recorded serialization failure occurs in the final campaigns.
Detailed logical cache misses/hits, pair/prune counts, full gate calls, serialization, queue and
transaction counters are in each campaign JSON and the aggregate, not inferred from elapsed time.

The observed parent application RSS is approximately 202–207 MiB; sampled DB cgroup peaks range
from about 499 to 751 MiB across final cases and include cache. The separate car OSRM container
has an approximately 58 MiB lifetime high-water mark. These are different measurement scopes,
not simultaneous summed memory. Final native verification samples show about 335 MiB for its
application/test-process group, 46 MiB for OR-Tools and 8.5 MiB for VROOM. Native short-process
samples are lower-bound observations, with the sampling qualification recorded in JSON.

### Comparison with the original benchmark

The original target's 512-booking/64-site result was 285.11 seconds, 1.796 admitted/s and decision
P95 227.20 seconds. It also returned 59,941 cells in 103 queries. This QA's repeat control has
the same physical acquisition count; its 373.76 seconds includes added independent checking
(20.33 seconds measured inside publication), different host/runtime conditions and separately
budgeted road processes. It is not evidence that application source regressed: source and
migrations are unchanged. The meaningful same-QA comparison is Control versus D above.
Both old raw campaigns and new artifacts are retained for independent inspection.

### Executed verification and failures

Corrected mandatory selection: **202 passed, 3 failed, 0 skipped**, three existing SQLAlchemy
cartesian-product warnings, 237.08 seconds pytest time (245.24 seconds supervised elapsed).
Failures:

1. Existing `test_bound_paid_booking_reuses_certificate_without_reoptimizing_incumbents`:
   `INVALID_FACILITY_HOURS`, reproduced in both full selections.
2. New controlled pre-horizon opening case: valid future admission remains PENDING.
3. New expired-shift custody case: healthy independent admission remains PENDING; the observation
   also records blocked next-shift opening and blocked post-expiry discharge.

The selection executes both real engines, real OSRM, independent pruning tests, migration
0030/history protection and downgrade/upgrade checks, source-head/lease races, competing capacity
and cancellation races, command replay, timeout/backoff, crash-after-commit recovery, actual
finite worker startup/cleanup, custody/transfer/handover and commercial source-cache restart.
Actual selection and resource counters are retained in the verification JSON. This is the
requested focused QA selection; it is not a rerun of every unrelated repository test.

No new Alembic/schema drift is introduced or observed by the executed schema checks. The strict
comparator continues to account for extension-owned objects through PostgreSQL catalogs and
explicitly validate the two pre-existing application indexes
`ix_collection_request_pickup_location_gist` and `ix_collection_request_planning_batch_id`.
Application/migration/ADR Git diffs from the target are empty.

All campaign JSON and runtime JSON are under `docs/benchmarks/GEOGRAPHICAL_QA_*`.
App: two CPUs / 4 GiB; PostgreSQL: two CPUs / 1 GiB; OSRM car: two CPUs / 512 MiB, one road thread.
These are disposable containers on a shared Windows Docker Desktop host, not a dedicated
performance laboratory or production sizing experiment. Versions and graph hashes are in
[`GEOGRAPHICAL_QA_MANIFEST.json`](benchmarks/GEOGRAPHICAL_QA_MANIFEST.json).

The control history capture adds a short read during checkout. A short two-case calendar probe
runs in the other disposable database during final D. Instance-wide DB CPU includes that probe;
per-database statistics and application cursor counters have their narrower stated scopes.
These scheduling details further limit strict CPU/latency comparisons; the measured acquisition
counts and correctness outcomes do not depend on claiming an otherwise idle host.

Measurements include sequential real service enqueue, four concurrent durable worker consumers
with one-intent quanta, certificate publication, actual booking creation and authenticated
synthetic payment capture. They exclude external gateway/HTTP/mobile network latency.
P50/P95/P99 use nearest-rank completed-intent latencies and include queueing. Pending intents
at the worker budget are right-censored; their eventual latency is not represented by the
completed subset's quantiles. UNKNOWN counts overlap PENDING; retries count intents with more
than one attempt. No arbitrary 128-booking check is added or bypassed.

Provider request latency includes HTTP decoding and strict provider validation. Returned cells
are square provider cells, not distinct retained arcs. `cache_misses_at_call` counts logical
requested **node-ID pairs** whose coordinate-pair entry is absent at the call boundary. Its
matching logical-hit count is `requested_arcs - cache_misses_at_call`. The production road-cache
hit counter deduplicates physical coordinate pairs, so it has a different denominator. Do not
derive a hit rate by mixing those counters. A miss never means unreachable.
Insertion pair/prune counters report the real kernel result. Native calls are counted separately
by engine; zero fallback is reported as zero, without claiming native large-scale capacity.

SQL times measure cursor round-trip work, not isolated PostgreSQL executor CPU. Transaction
counters come from `pg_stat_database`, whose asynchronous flush can lag. SQLSTATE 40001/40P01
are counted in the final harness; certificate TypeAdapter serialization is measured separately
from total SQL time, not falsely described as all driver/JSON serialization. Sampled DB memory
includes PostgreSQL cache. Parent and child RSS are separate; child RSS is not automatically
attributed to a native solver. Runtime OSRM counters are cumulative cgroup CPU deltas and
explicit **container-lifetime** memory high-water marks, not falsely reset per-campaign peaks.
Native verification samples the processes at 20 ms and records that short processes may be missed.

The original report ran OSRM inside the test container; this QA separates its cgroup to measure
road CPU/memory. Therefore original aggregate application CPU is not directly comparable with
the new application-only CPU counter. The earlier `*_exploratory*` runs are retained for audit;
their instrumentation was finalized during QA and some overlapped small correctness probes.
Use the final Control/A/B/C/E results for like-harness comparisons. D's bounded run records
its exact harness counters and limitations rather than being silently replaced by extrapolation.

Fresh OSRM car+bicycle processes reach readiness in 0.068 and 0.118 seconds on this tiny,
already cached filesystem graph. The manifest records both repeats and child memory/CPU.
This excludes image pull/boot, full Delhi extraction/loading, filesystem cold I/O and production
orchestration startup. Cold/warm exact sparse requests, new positions, both profiles, source
changes, process-cache loss and finite-worker process restart are tested explicitly.

The one-way-site field describes original input-way tags; a profile may snap an input vertex
to another legal road. It does not independently certify the snapped segment's one-way status.
Actual directed asymmetry and null arcs come from OSRM, and the synthetic topology separately
verifies directed bridge/restricted-access behavior. All large campaigns reuse a running road
process and the host filesystem cache. “Cold” means fresh application read/arc caches, not an
evicted OS filesystem cache, cold PostgreSQL buffers or full-city graph load.

## Execution and remaining release work

The exact per-campaign Docker arguments are retained in their runtime files. The reusable
PowerShell campaign runner is [`run-geographical-qa.ps1`](../tests/routing/run-geographical-qa.ps1).
The explicit verification runner fails when a mandatory dependency is unavailable or **any**
selected test is skipped. Ordinary repository collection retains explicit QA opt-in markers;
they are not an authorization to skip missing dependencies during qualification.

Remaining production qualification requires resolving the custody/admission failures and OSRM
provider blocker, then repeating the evidence on a representative full Delhi graph and approved
physical vehicle profiles. Sustained arrival rates, real telemetry churn, independent regional
lanes, real traffic, different region sizes and production infrastructure are not qualified here.
No production algorithm correction is included or implied by this QA report.

## Reproduction commands and artifact guide

From the QA checkout, with Docker Desktop available:

```powershell
# If the recorded local native image is unavailable, build the existing test images.
docker build --target operational-tests -f tests/routing/Dockerfile -t tirodhan-routing-tests:increment-6 .
docker build -f tests/routing/Dockerfile.regional -t tirodhan-routing-tests:increment-8 .

# Fresh, task-owned resources only; omit -Setup when reusing verified QA resources.
./tests/routing/run-geographical-qa.ps1 -Setup -Budget 600 -MixedBudget 180

# The campaign runner records failures and exits nonzero for incomplete campaigns.
# Run verification separately, even after that expected campaign failure.
$qaVerification = (Get-Content docs/benchmarks/GEOGRAPHICAL_QA_VERIFICATION.runtime.json -Raw | ConvertFrom-Json).docker_arguments
docker @qaVerification

# Aggregate actual evidence; this does not turn a failed qualification into a pass.
python tests/routing/geographical_results.py
```

The runtime arrays retain the exact executed arguments, including required graph-version
provenance, explicit native/QA opt-in flags, volumes, database URL and selected tests.
Change only the checkout bind path when reproducing on another host. Local test credentials
are disposable fixtures. No cloud account, real payment gateway, paid matrix API or customer
data is required. A newly rebuilt image can have different transitive packages; the observed
image IDs, solver versions, package inventory and graph hashes qualify the recorded results.

`geographical_inventory.py --output ...` runs inside the same native image/network with the
`/graphs` mount and test database URL; it records every generated graph-file hash and measures
two fresh-process car/bicycle startups. `geographical_preflight.py --output ...` regenerates
the public fixture and the raw directed-table summaries. `geographical_fixture.py --output ...`
can generate offline candidate provenance without a routing service.

`geographical_history.py` contains the exact single-statement SQL used to capture the control's
history before fixture teardown. Its CLI audits a captured JSON document without application
snapshot/proof composition code. The raw synthetic UUID document is local diagnostic scratch;
the committed aggregate retains its SHA-256, revision counts and zero-missing-pair result.
The ordinary campaign replay also checks source coverage directly at publication.

`GEOGRAPHICAL_QA_RESULTS.json` aggregates all six final campaign results, their exact runtime
arguments, verification and comparison evidence. Individual JSON files retain queue samples,
all measurement counters and the independent witness. `*_exploratory*` and
`GEOGRAPHICAL_QA_VERIFICATION_INITIAL.json` preserve earlier observations; they are not silently
reported as the final corrected test run.

## Quality boundary

The full repository formatter encounters a **pre-existing invalid UTF-8 byte** in
`docs/COMMERCIAL_ADMISSION_SCALABILITY.md` at normalized byte offset 28,450, line 417 (`0x96`).
The normalized file is byte-identical to the target Git blob, SHA-256
`c55d85f34763ce960e05ba4f7f564a74ebe74d6b2507d2ab5fb625f7c339a299`.
This QA leaves that target document unchanged. Full-repository formatting cannot be reported
as passing; Python-only formatting is checked separately, along with Ruff and application mypy.
The initial native run also omitted `TIRODHAN_OSRM_GRAPH_VERSION`, causing four configuration
failures. The final invocation supplies it, and the runner now rejects missing provenance
before starting pytest. Initial failures remain in the evidence rather than being concealed.
