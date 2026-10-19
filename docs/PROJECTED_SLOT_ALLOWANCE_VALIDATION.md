# Projected slot allowance validation

Date: 19 October 2026. Base `7f3b19a11e2e216e8a806a529a9418a1b5a81281`.
Branch: `codex/commercial-admission-scalability`. Local implementation; no production activation.

## Implemented boundary

Customer commercial routing feasibility is switched off in code. The old preparation POST,
old rider commercial-offer read/accept path and dedicated admission worker refuse new work;
the fleet worker no longer consumes the historical admission queue. Historical successful
reservations and financial/custody history remain. The replacement is background forecast,
atomic slot hold, then existing booking/payment. Checkout does not acquire a road matrix,
run a solver, run insertion search or refresh stale projections synchronously.

The non-trained workload estimator and transactional constraints are described in
[PROJECTED_SLOT_ALLOWANCES.md](PROJECTED_SLOT_ALLOWANCES.md). The independent operational
route validator remains authoritative for execution; forecast inventory is not a route proof.

## Changed modules and schema

- New `allowance_estimator.py`, `allowance_projection.py`, `allowance_policy.py` and
  `slot_holds.py`: integer footprints, coupled time budgets, interval calendars, retained
  policy versions, consistent publication and atomic holds.
- `slot_projection.py`, `capacity_models.py`, `orchestration_models.py` and
  `operational_commands.py`: read model, typed inventory/policy/debit persistence and manager
  configuration under existing identity/idempotency conventions.
- `api/routes/routing_orchestration.py`, `workers/routing_admission.py`,
  `workers/routing_fleet.py`, `workers/routing_availability.py`: new hold contract, retirement
  of commercial preparation callers and existing finite refresh worker terminology.
- `admission.py`, `admission_store.py`: historical exact-certificate helpers explicitly
  distinguish forecast holds, preventing NULL from being decoded as a route proof.
- Migration `0032_projected_slot_allowances`: three new tables, projection inventory columns,
  reservation admission basis/duty/policy fields, real FKs, immutable history, conditional debit,
  deferred completeness trigger and extended capacity-removal guard. Earlier migrations unchanged.
- New estimator/property, hold/concurrency/recovery and opt-in benchmark tests; two existing
  integration modules updated for the approved boundary. Architecture/domain/schema/ER/idempotency
  documents and ADR-004/006/015 record the implemented policy change.

No dependency, infrastructure or financial/custody lifecycle schema is added or replaced.

## Algorithms and bounds

Workload is deterministic aggregate workload accounting, interval union/subtraction and
multi-resource inventory consumption. It does not fit a statistical distribution, train a
model or infer a reliability percentage. Explicit local travel/time-band assumptions must
come from operations and later be checked against actual outcomes.

Let N be live regional obligations, V eligible shifts, T active route tasks, B calendar
exclusions, S published slots and K buckets touched by one footprint. S is at most 224
(seven operating dates); configured travel and existing service bounds keep K bounded.
Footprint construction is O(K); each debit vector conserves integer duty seconds. Allowance
is the minimum integral headroom across its pickup constraint and every affected duty bucket.
This is a necessary workload budget, not a sufficient solution to VRPTW.

Refresh reads regional live facts, sorts obligations/history and interval endpoints, and
compares each shift/calendar with the bounded slot horizon. Its cost grows with N/V/T/B,
not a road matrix of N squared arcs; nested calendar/facility intersections still matter
for regions with large histories/calendars. A hold reads O(S) inventory and updates O(S)
source/allowance stamps with O(K) debits. It never loads all regional households or an N-by-N
matrix. The region is one transactional writer; concurrent customers queue at its region/head
lock. This is a measurable throughput limit, not unlimited horizontal scaling within one region.

SQL conditional debits prevent overspending the current **modelled** inventory. Publication
cannot overwrite a concurrent hold: source fencing and consistent snapshots abort the loser.
Accepted work is retained if a new forecast has less supply than existing usage. Neither
transactional arithmetic nor an interval screen proves that all sold work has a physical route.

## Reproduction

Use the existing pinned native image built by `tests/routing/run-regional.ps1`. Full native
regression command: `./tests/routing/run-regional.ps1 -SkipBuild -FullSuite`. It provisions and
removes its own PostgreSQL/PostGIS and car/bicycle fixture graphs. Solver versions are OSRM
6.0.0, VROOM 1.15.0 and OR-Tools 9.14.6206; Python in that image is 3.13.5.

For the new benchmark, start a disposable database and run the following from the repository
in PowerShell. Use a unique container name if running another experiment. No graph is started.
The image must already exist, or build it with the existing routing Dockerfiles first.

```powershell
$slotDb = 'tirodhan-slot-allowance-benchmark-local'
docker run -d --name $slotDb --cpus 2 --memory 1g `
  -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=tirodhan_test postgis/postgis:17-3.5
# Wait for: docker exec $slotDb pg_isready -U postgres -d tirodhan_test
try {
  docker run --rm --cpus 2 --memory 4g --network "container:$slotDb" `
    -v "${PWD}:/repo:ro" -e PYTHONPATH=/repo/src `
    -e TIRODHAN_HYPOTHESIS_DATABASE=/tmp/hypothesis `
    -e TIRODHAN_TEST_DATABASE_URL=postgresql+asyncpg://postgres:local-test-only@127.0.0.1:5432/tirodhan_test `
    -e TIRODHAN_SLOT_ALLOWANCE_BENCHMARK=1 -e TIRODHAN_ADMISSION_COUNT=192 `
    -e TIRODHAN_ADMISSION_VEHICLES=80 -e TIRODHAN_ADMISSION_CONCURRENCY=1 `
    tirodhan-routing-tests:increment-8 python -m pytest `
    tests/integration/test_slot_allowance_benchmark.py -q -s -p no:cacheprovider
} finally {
  docker rm -fv $slotDb
}
```

Replace concurrency with 8 for the competing-customer run. The fixture creates synthetic
identities, serviceability and declared shifts, then calls actual hold, booking, payment
preparation and authenticated synthetic capture services. It seeds no new accepted bookings
or forecast reservations. Gateway/network latency is excluded. All holds precede checkout,
matching the recorded historical non-interleaved workload. During this test, legacy reserve,
OSRM acquisition and both native optimizers are patched to fail if called.

## Executed correctness checks

Initial focused command, against disposable PostgreSQL/PostGIS with the CI extension family
(PostGIS, topology, fuzzystrmatch and TIGER/geocoder) installed:

```text
python -m pytest tests/integration/test_slot_holds.py tests/routing/test_allowance_estimator.py
  tests/integration/test_regional_capacity_recovery.py -q --tb=short -p no:cacheprovider
```

Result: **38 passed, 5 warnings in 106.12 seconds**, no skips. The warnings are the existing
booking/reservation PK-filtered cartesian-product SQLAlchemy warning; no financial query was
changed in this increment. This run covers both new hold APIs and retired commercial/flexible
APIs, retired dedicated/fleet worker paths, exact replay, concurrent final allowance, competing
neighbouring slots, expiry/cancellation/payment recovery, source drift, refresh/hold conflict,
partial-write rollback, deferred debit enforcement, policy history, service-class bounds,
shift-close protection and collected-cargo exclusion after loss of its discharge task.
The existing recovery suite covers one-region ownership and manager replacement/custody races.
The estimator suite includes independent per-second interval and footprint conservation oracles.

The later expanded run on the bounded inventory query, including both full-suite timeout
failures, returned **42 passed, 2 failed, 5 warnings in 107.76 seconds**. Both failures were
new transfer-race fixture errors: it attempted transfer before shift start, which the domain
correctly rejects before reaching the lock barrier. After giving the synthetic scenario
pre-shift confirmation and an active-shift clock, all four close/transfer overlap cases passed:
**4 passed in 18.54 seconds**. Both command orders are observed simultaneously waiting in
PostgreSQL; exactly one hold/withdrawal commits. The strengthened populated migration case
then passed separately: **1 passed in 8.08 seconds**. Overall, the 44 distinct targeted cases
passed across focused/follow-up runs (two ownership cases overlap). This is not a claim that
the entire final tree was rerun as one green full-suite invocation.

Migration 0032 was upgraded on disposable PostgreSQL. Populated financial-history downgrade to
0031 and upgrade to head passed strict schema comparison, PaymentAttempt schema/defaults and
all three initiation-term trigger checks. The existing extension-ownership catalog filter is
unchanged. Strict comparison still permits exactly **two** application metadata omissions:
`ix_collection_request_pickup_location_gist` and `ix_collection_request_planning_batch_id`,
each also checked explicitly for columns/method/uniqueness. `spatial_ref_sys` and other owned
extension objects are excluded by catalog ownership. No new application drift was accepted.

Ruff check passed; Ruff format check passed; strict mypy passed (227 source files);
`git diff --check` passed. No formatter/type exclusions were added.

The broad native full run completed with **1,508 passed, 3 failed, 12 skipped, 9 warnings in
2,246.07 seconds**. It began before the final fleet/cargo/API and bounded-query refinements;
final deltas have the targeted coverage above. The 12 skips are explicit opt-in commercial,
batch/orchestration and 2,000-booking benchmark campaigns. The new benchmark was subsequently
run explicitly in reference/sequential/concurrent modes, each passing.

Full-run failures:

- `test_routing_fleet.py::test_native_collective_fleet_and_moving_cargo[VROOM]`: five-second
  `MATRIX_TIMEOUT`. Passed in the isolated expanded follow-up with the same pinned real graph.
- `test_financial_provider_boundaries.py::test_http_ack_waits_for_broker_and_exact_replay_keeps_minimal_identity`:
  one-second publisher-entry wait timed out. Passed in that follow-up. These are observed
  timing failures in unchanged paths, not proven new defects or removed assertions.
- `test_twofactor.py::test_timeout_traceback_and_http_logging_hide_sensitive_url`: reproducible
  Python 3.13 traceback/source assertion described below; still fails there.

An unchanged OTP regression was independently reproduced under native Python 3.13.5:
`tests/unit/test_twofactor.py::test_timeout_traceback_and_http_logging_hide_sensitive_url`.
Boolean-only diagnostics establish that `traceback.format_exception` includes the synthetic
OTP literal from the test's own multiline source callsite; key/session/phone remain absent.
The same unchanged test passed under host Python 3.10.5 (8.66 seconds). Both its test and provider
source are identical to the base commit. It remains an unrelated runtime/test compatibility
failure, not a newly accepted leak or a green full-suite claim; no identity code was modified.

## Measured admission and projection performance

Fresh serial reference: existing internal exact-certificate benchmark on this build, including
real OSRM/native validation, **not** a re-enabled public API. Reproduce with
`./tests/routing/run-regional.ps1 -SkipBuild -CommercialAdmission -AdmissionCount 192
-AdmissionVehicles 80 -AdmissionConcurrency 1 -AdmissionBudget 300`. Projection runs use the
new command above. All modes use 192 bookings, 80 vehicles, 64 public sites, 70% morning-slot
concentration, 180-second pickup and 60-second discharge. New travel assumptions are synthetic.

| Metric | Exact reference | Projection, concurrency 1 | Projection, concurrency 8 |
| --- | ---: | ---: | ---: |
| Accepted / attempted | 192 / 192 | 192 / 192 | 192 / 192 |
| End-to-end batch seconds | 100.81 | 24.41 | 27.38 |
| Accepted bookings / second | 1.90 | 7.86 | 7.01 |
| Queued decision P50 / P95 seconds | 35.93 / 76.67 | 5.15 / 9.52 | 5.93 / 11.28 |
| Hold service P50 / P95 milliseconds | n/a | 48 / 63 | 524 / 806 |
| Checkout/capture P50 / P95 milliseconds | 76 / 114 | 71 / 98 | 75 / 105 |
| Application SQL calls | 71,028 | 15,379 | 15,379 |
| Database CPU seconds | 17.22 | 4.81 | 7.21 |
| DB average fraction of two cores | 8.54% | 9.85% | 13.17% |
| Application CPU seconds | 71.38 | 16.66 | 18.01 |
| Database memory at end, MiB | 399 | 463 | 511 |
| Failures / pending / deadlocks | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| Road-matrix calls in new path | n/a | 0 | 0 |
| Optimizer calls in new path | n/a | 0 | 0 |

The sequential batch is 4.13 times faster here. Correctness semantics intentionally differ:
exact feasibility certification versus modelled workload reservation. This is not a like-for-like
routing-algorithm quality comparison. Queued decision includes earlier work; hold service includes
DB lock waits after the benchmark semaphore. Concurrency 8 increases regional lock latency rather
than yielding linear scaling. Summed concurrent SQL time is not database CPU or batch wall time.

After 192 accepted bookings, 30 seven-date/224-slot refreshes measured P50/P95 179/307 ms
sequential and 213/291 ms after the concurrent run. Slot reads measured 6/9 ms and 8/15 ms.
These include database access. Hold reads have an explicit indexed time bound so retained audit
buckets cannot expand their working set.

Actual results, assumptions and limitations are retained in
[PROJECTED_SLOT_ALLOWANCE_RESULTS.json](benchmarks/PROJECTED_SLOT_ALLOWANCE_RESULTS.json).
Each mode is one run on disposable PostgreSQL/PostGIS (2 CPUs, 1 GiB) and application container
(2 CPUs, 4 GiB). Test loads ran serially; other user containers stayed unchanged. Database buffers
warm across modes; UUIDs/wall time vary. Gateway/mobile/HTTP latency is excluded. PostgreSQL
statistics are asynchronous; rollback includes ordinary read cleanup. No prediction reliability
percentage, production SLA or statistical significance is established.

## Activation requirements and limitations

Existing production enablement flags remain unchanged. A deployment needs client migration to
slot holds, regional manager policy configuration, retirement of any old admission job schedule
and event-driven/periodic execution of the existing availability worker. No such deployment
or scheduler/infrastructure change is part of this commit.

Missing/stale inventory fails closed for **new holds**; it never revokes an earlier hold or
paid commitment. Concurrent source changes or interrupted refresh can temporarily produce UNKNOWN.
Cancellation/expiry release is eventually recomputed; it does not gamble on immediate reuse.
Policy/debit history is retained; populated-feature downgrade requires an explicit preservation
plan and is refused automatically. Existing capacity-removal guards are intentionally conservative.

Only the published standard pickup/discharge class is sold in this increment. Larger requested
service times are rejected explicitly. Physical vehicle load is excluded from the forecast,
while existing manifest snapshots and operational physical-load checks remain. Multiple service
classes and empirical regional/time-band calibration remain work to measure, not hidden defaults.
There is no 128-booking limit in this new customer hold path. The seven-date sales calendar,
finite refresh cadence, region lock serialization, query/history growth and prediction accuracy
remain real bounds. No trained model or additional recurring infrastructure is required.

Continuous interleaved arrivals and payment-driven invalidation/refresh cadence still need a
production-like load campaign before enabling sales. This burst benchmark does not establish
visible availability under every arrival rate or delayed refresh schedule.

This is ready for independent code/transaction review with the executed evidence above;
production forecast accuracy has not been established by synthetic arithmetic or concurrency tests.
