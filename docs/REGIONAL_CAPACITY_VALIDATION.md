# Regional capacity and recovery — validation record

Base: `d9b676d74fd84394789b1909c2afa0335f5b69a9`.
Branch: `codex/commercial-admission-scalability`.
This record describes the local implementation, not production enablement or independent approval.

## Delivered surface

The implementation and policy boundaries are specified in
[REGIONAL_CAPACITY_AND_RECOVERY.md](REGIONAL_CAPACITY_AND_RECOVERY.md).

Changed production modules:

- `planning/routing/osrm.py`, new `osrm_rectangular.py`: exact directed OSRM Table rectangles;
  `scheduling.py`: an unused empty vehicle does not require an impossible return journey.
- `routing_operations/admission.py`, `admission_state.py`, `snapshot.py`: isolate explicitly
  quarantined resources, retain their actual obligations, and intersect facility calendars.
- `admission_roads.py`: select rectangular sparse acquisition or dense useful blocks.
- New `capacity_models.py`, `regional_commands.py`, `capacity_commitments.py`: single-owner
  adjacent-region transfer, immutable audit and certified-commitment shift-close protection.
- New `replacement_commands.py`, `replacements.py`: rider request, manager decision and
  independently validated recommendation; existing reassignment and evidenced cargo transfer.
- `authority.py`, `operational_commands.py`, `commands.py`, `regional_projection.py`,
  `orchestration_models.py`: transactional integration, source fencing and ordinary activation.
- New `slot_projection.py`, `capacity_maintenance.py`, `workers/routing_availability.py`;
  `workers/routing_admission.py`, `workers/routing_regional.py`: advisory read model and finite
  due-region maintenance, with authoritative processing before advisory recommendation work.
- `api/routes/routing_orchestration.py`: owned-context advisory slots, manager review and paged
  membership/version discovery, using existing live role authorization and command APIs.

Migration `0031_regional_capacity_recovery` adds `operating_region_vehicle.version`, five tables
(`region_capacity_transfer`, `rider_replacement`, `rider_replacement_decision`,
`regional_slot_projection`, `regional_slot_refresh`) and their guards/indexes/invalidation functions.
`migrations/env.py` registers the models. Financial/custody/evidence tables are reused unchanged.
Downgrade refuses to discard populated replacement/transfer audit; empty-feature round trips
preserve populated financial history, PaymentAttempt defaults/schema and initiation-term triggers.

Tests cover the new ownership/replacement/projection/migration behavior and native rectangles;
the existing benchmark and local disposable harness accept the controlled matrix-mode switch.
Architecture, domain/schema/ER/idempotency documents and ADR-004/006/015 describe implemented policy.
The earlier commercial-admission report also receives a one-character invalid UTF-8 repair so
repository-wide formatting can execute.

## Algorithms and correctness

This increment retains the base's exact fixed-order pickup/discharge insertion, forward/backward
time-window propagation, cumulative mass/volume checks, bounded VROOM/OR-Tools repair and independent
feasibility validator. Recommendation ranks feasible committed resources before eligible flexible
capacity, then incremental travel (total travel if no comparable proof). A finite search does not
prove global optimality or infeasibility. Existing numerical safety provisions are unchanged;
no additional blanket reservation percentage is introduced.

The new slot sweep unions overlapping breaks and integrates eligible-resource counts over each
slot: `C(region,slot) = integral sum_v available_v(t) dt`. Endpoint sorting/integration and duty
overlap lookup cost `O((V+B+S) log(V+B+S))`; database aggregation also costs work proportional to
matching bookings/routes/custody records. It does not solve routing or issue a certificate.

Sparse road stars cost `2b` provider cells instead of `b²`; dense useful blocks retain square
requests. All durations/distances are actual directed provider values under the same graph,
profile and canonical snap provenance. A 32-node native regression compares every requested
directed arc with the dense response. Negative values, fallbacks, timeout, inconsistent
reachability, changed snapping and interruption preserve safe failure. Overall sparse acquisition
and accumulated proofs still have quadratic worst cases; geographical partitions cannot bound
arbitrary demand density.

Publication uses PostgreSQL transactions, existing Payment-first/resource locks, sorted regional
source fences and explicit row versions. Transfers are SERIALIZABLE and cannot move committed
capacity; stale certificates/projections cannot authorize spending it twice. Manager approval
does not alter physical custody. Existing validated transfer appends custody history and changes
affected route recovery state atomically. Normal activation produces the subsequent concrete
route revision. No native solver or advisory endpoint can assign a rider or spend capacity.

Primary references: [OSRM Table API](https://github.com/Project-OSRM/osrm-backend/blob/v6.0.0/docs/http.md),
[OR-Tools time windows](https://developers.google.com/optimization/routing/vrptw),
[Uber H3](https://www.uber.com/us/en/blog/h3/), and the base algorithm references in
[COMMERCIAL_ADMISSION_SCALABILITY.md](COMMERCIAL_ADMISSION_SCALABILITY.md).

## Environment and reproducibility

Disposable `postgis/postgis:17-3.5` database: two CPUs / 1 GiB; native test container: two CPUs /
4 GiB. PostGIS, topology, fuzzystrmatch and TIGER/geocoder extensions are installed. The existing
strict extension-ownership-aware metadata assertion is retained: no broad drift filter was added.
The original known PostGIS/application-index differences remain explicitly checked.

Pinned existing routing image: `tirodhan-routing-tests:increment-8`. Graph preparation uses the
OSRM image digest already pinned in `tests/routing/run-regional.ps1` and public Delhi fixture SHA256
`4f36f7e749aef6f21128288ab8ca6447666a506a440d74db778a079974a71b49`.
Measurements use 64 public road sites in that small clipped graph, not a city-scale graph.
Actual binaries: Python 3.13.5, OSRM 6.0.0, VROOM 1.15.0, OR-Tools 9.14.6206.
Host quality checks use the repository virtual environment's Python 3.10.5.

## Executed validation

| Run | Actual result |
|---|---|
| Full repository native-enabled run, stopped at first failure | 772 passed, 12 skipped, 1 failed; 1,520.12 s |
| Isolated native fleet plus new ownership/matrix/calendar/insertion regressions | 41 passed; 73.34 s |
| Remaining integration/routing/unit/property/deployment selection | 697 passed, 1 failed; 158.51 s |
| Final fresh-schema ownership/admission/insertion/native-matrix/calendar regressions | 36 passed, 1 new-test fixture assertion failed; 68.11 s |
| Corrected two-resource HTTP pagination fixture plus final schema/financial round trip | 2 passed; 9.78 s |
| Final ownership/custody/HTTP/schema/calendar and recommendation-outage recovery run | 16 passed; 35.78 s |
| Fleet crash/replay, lease, cancellation/refund and physical-obligation reliability cases | 17 passed; 33.65 s |
| OTP traceback assertion, unchanged, host Python 3.10.5 | 1 passed; 5.18 s |
| Same OTP assertion isolated in native Python 3.13.5 image | 1 failed; 9.43 s |
| Explicit 192-booking square benchmark | 1 passed; 114.44 s including setup |
| Explicit 192-booking adaptive benchmark plus 30 projection refresh/read pairs | 1 passed; 69.90 s including setup |
| Explicit four-consumer, interleaved checkout/capture campaign | 1 passed; 46.45 s including setup |

The initial full run stopped on `test_native_collective_fleet_and_moving_cargo[VROOM]`
with `MATRIX_TIMEOUT`. All three engine-availability variants passed on the isolated fleet
rerun. This observation is recorded as a non-reproduced timeout, not erased or attributed to
an upstream defect without evidence. The remaining suite was then executed separately.

Collection-to-JUnit reconciliation accounts for **all 1,504 collected test IDs** across these
split runs: latest native-image outcomes are **1,491 passed, 12 skipped, 1 failed**. This is
coverage across runs, not a claim of one uninterrupted green full-suite run. The remaining
failure is the unchanged OTP runtime-specific assertion below. Per-run counts, skipped IDs
and the failing ID are committed in
[REGIONAL_CAPACITY_RECOVERY_VALIDATION.json](benchmarks/REGIONAL_CAPACITY_RECOVERY_VALIDATION.json).

`tests/unit/test_twofactor.py::test_timeout_traceback_and_http_logging_hide_sensitive_url`
fails reproducibly under Python 3.13.5 because formatted traceback source context includes the
synthetic OTP literal in the test callsite itself. A separate probe confirmed that only that
source literal matched; the key, session and phone did not appear. It passes on Python 3.10.5.
The test and provider are byte-unchanged from the base. No authentication code/assertion is
weakened to make the native image green. The repository CI selects Python 3.12; this increment
does not claim a new GitHub CI result or a fully green Python 3.13 suite.

The new HTTP test initially requested a second page from a fixture containing one vehicle.
Its corrected two-vehicle fixture verifies distinct cursor pages and the terminal cursor,
along with owned-context access, missing authentication and manager-only capacity access.
Final schema round-trip validation includes the new regional recommendation due index.

Twelve full-run skips are explicit opt-in performance campaigns, not missing native engines:
one commercial campaign, four orchestration batch campaigns, four orchestration campaigns and
three expensive regional campaigns. The commercial campaign is executed explicitly above.
Existing warnings include the independently primary-key-filtered booking/reservation SELECT
cartesian-product warning, Firebase multicast deprecation, and a Python 3.13 coroutine-close
warning in the unchanged financial provider oversize test. These are reported, not suppressed.

New tests exercise replay, conflicting transfers, transfer-versus-admission arbitration, paid
promises before alias refresh, shift-close protection, stale recommendation rejection, manager
rejection, approved re-search, ordinary reassignment activation, unvalidated/validated physical
transfer, expired loaded resources, source invalidation/expiry/reconciliation, past facilities,
empty unusable vehicles, native directed values and interrupted/invalid road acquisition.
The calendar property test compares the interval sweep against independent per-second occupancy.
Recommendation matrix outage/recovery verifies durable retry state and that original work is
never released by an unsuccessful recommendation or recommendation publication alone.

Final checks: Ruff passes, Ruff formatting passes, mypy passes for 223 source files, and
`git diff --check` passes. No tests were weakened/skipped to hide a defect. Earlier exploratory
failures (before corrections) included the new snap-token encoding mismatch and the independently
reproduced existing unused-vehicle schedule defect; both have passing final regressions.

## Measured comparison

Raw aggregate counters and limitations are committed in
[REGIONAL_CAPACITY_RECOVERY_RESULTS.json](benchmarks/REGIONAL_CAPACITY_RECOVERY_RESULTS.json).
The workflow enqueues actual customer intents, certifies capacity, creates bookings, prepares
payment attempts and processes authenticated synthetic payment acceptance. It does not insert
accepted bookings directly. Seventy percent of demand targets the first two 30-minute slots;
the remainder covers the next four hours. Eighty newly declared committed vehicles have
10 kg / 10 litre capacities; loads are 1–2.5 kg / 1–2 litres; service is 180 s pickup + 60 s discharge.

| Metric | Square capability | Adaptive rectangles |
|---|---:|---:|
| Accepted / submitted | 192 / 192 | 192 / 192 |
| Final failures / pending | 0 / 0 | 0 / 0 |
| Workflow time, s | 99.72 | 56.47 |
| Accepted/s | 1.93 | 3.40 |
| Enqueue-to-decision P50 / P95, s | 34.71 / 76.65 | 21.76 / 41.33 |
| Certification-attempt P50 / P95, s | 0.414 / 0.644 | 0.172 / 0.406 |
| Checkout/capture P50 / P95, s | 0.0737 / 0.1002 | 0.0555 / 0.0656 |
| Provider cells | 60,791 | 4,499 |
| Provider HTTP queries | 101 | 197 |
| SQL calls / measured cursor seconds | 71,028 / 36.44 | 42,464 / 18.32 |
| Database CPU, s | 17.85 | 10.31 |
| Database mean fraction of allocated two cores | 8.95% | 9.13% |
| Application CPU, s | 72.47 | 39.73 |
| Peak Python RSS, MiB | 199.05 | 196.84 |
| Deadlocks / temporary spill bytes | 0 / 0 | 0 / 0 |

Provider cells decrease by 92.6%; HTTP requests increase because a sparse star uses two
rectangles. Observed throughput is 1.77 times the square run. This is one paired local
observation, with real clock progression and random resource UUIDs; the adaptive run also
benefits from previously warmed database shared buffers. SQL/latency changes are measured
outcomes, not a claim that rectangle transport alone causes every difference. PostgreSQL
rollback counters include normal read-session cleanup and do not count rejected bookings.
Database end memory includes filesystem/shared cache; it is not backend RSS.

For 192 accepted bookings, 30 refresh/read pairs over 193 current/future slot rows measured:
refresh P50/P95 **45.05 / 61.84 ms**; read P50/P95 **3.85 / 5.35 ms**. Calendar-date progression
changes the number of remaining rows in the seven-date grid. The read model never invokes
OSRM/VROOM/OR-Tools. This does not establish performance on millions of retained historical rows.

A separate 64-booking / 20-vehicle campaign with four competing consumers and interleaved booking
creation/payment capture accepted **64/64**, with zero terminal failures/pending work and zero
deadlocks/spills. Elapsed workflow time was 38.24 s, throughput 1.67 accepted/s, decision P50/P95
12.22/28.70 s, checkout/capture P50/P95 66.91/174.81 ms and mean database usage 7.60% of two cores.
It exercised 80 leaf delta reads and one full read. Four consumers still share one authoritative
regional writer; this is correctness/recovery coverage, not evidence for fourfold throughput.
All three explicit campaigns accepted **448/448** commercial workflows.

Reproduce a fresh disposable benchmark with the existing harness:

```powershell
./tests/routing/run-regional.ps1 -SkipBuild -CommercialAdmission -AdmissionCount 192 -AdmissionVehicles 80 -AdmissionBudget 600 -AdmissionMatrixMode square
./tests/routing/run-regional.ps1 -SkipBuild -CommercialAdmission -AdmissionCount 192 -AdmissionVehicles 80 -AdmissionBudget 600 -AdmissionMatrixMode adaptive_rectangular -SlotProjectionBenchmark
```

These compare the final implementation with its optional rectangle capability exposed/hidden,
isolating transport changes. They are not an exact pre-feature checkout comparison. The exact
base's previously recorded 64/128/256/512 campaigns remain in its original report and are not
relabeled as new measurements.

## Release boundaries

The former 128-booking commercial ceiling was already removed at the base and is not restored.
Both new 192-booking campaigns exercise that fact through the actual commercial/payment path.
Worker quanta, cursor pages, local-search budgets and memory limits remain execution controls;
exhaustion must recover/retry or return UNKNOWN, not discard accepted obligations.

No production flag, scheduler, Azure resource, paid API, Redis or new deployment service is added.
The finite availability job still needs scheduling through the approved background execution
architecture when activation is authorized. Regions remain manually configured H3 groups;
automatic equal-area partitioning and dynamic boundary management are not included.

Same-vehicle successor-shift handover is not implemented; existing explicit custody recovery uses
a different active receiving vehicle. Recommendations do not compute a rendezvous or reserve the
recommended rider. An approved uncollected request can be re-searched while preserving its old
constraint until a fresh source-fenced result exists. Search/facility/provider failure retains
durable recovery state and customer/custody commitments.

The reproducible upstream OSRM 6.0 phantom negative-distance defect remains a production blocker;
strict rejection and recovery are retained. Accurate rectangles reduce unnecessary cells but do
not correct upstream graph arithmetic. Whole-city graph memory, dense-region queue latency,
proof/history amplification, durable scheduling cadence and operational calibration still need
launch-scale evidence. This increment does not claim unlimited or production-certified throughput.

The recommendation search is capped at 30 seconds per maintenance call and processes one due
request per region; unavailable advice remains recoverable rather than reserving an unproven
rider. Slot projections expire after the default 60 seconds and cover at most seven operating
dates. Repeated contention may leave them UNKNOWN. Final certification still serializes commits
per region and retains existing execution/memory/provider-format limits. These are measurable
technical limits, not a hidden 128-booking eligibility ceiling.

There is no substantial deviation from the approved infrastructure/ownership/custody decisions.
The advisory read model deliberately reports pressure rather than inventing an exact count-only
capacity certificate. Immediate custody recovery marks the receiver's operational recovery state;
its concrete route is subsequently produced by normal validated activation, outside custody locks.

Readiness: **ready for independent implementation review, not production activation**. Critical-path
remaining work is upstream road-provider qualification/correction, whole-city workload measurement
and the previously approved controlled activation/scheduling runbook. Budget roughly 1–2 engineering
days to qualify a specific upstream fix/build and rerun the diagnostic, but a reliable defect-fix
ETA cannot be promised before such a candidate exists. Allow roughly 2–4 engineering days for a
representative whole-city performance/calibration campaign after graph and workload inputs are
available. These are planning estimates, not tested outcomes or authority to add infrastructure.
No PR, merge, deployment or additional increment is part of this delivery.
