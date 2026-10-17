# Routing reliability and commercial admission scalability

Date: 17 October 2026. Base: `fb28affdfae1b94b222a8a5e2cfa5878cbce5976`.
Branch: `codex/routing-reliability-admission-scalability`.
Production routing remains disabled; this delivery provisions no production infrastructure.

## Findings and disposition

1. **OSRM 6.0 distance defect: root cause verified, upstream correction still a release blocker.**
   The application provider correctly rejects invalid raw output. No clamp, absolute value,
   coordinate perturbation, invented road value, alternate paid provider or unverified native
   patch is introduced. Durable commercial retries now survive road-provider failures.
2. **Commercial admission: measured wasted reoptimization and same-region preparation conflicts
   corrected.** Whole-fleet reoptimization is no longer the incremental certificate fast path.
   Regional preparation has a durable owner, with native work outside transactions. Customer
   capacity promises retain the independent validator and SERIALIZABLE live source/version fence.
   **The bounded regional certificate's 128-request horizon is still a high-volume release limit.**
   This change does not establish support for 2,000 simultaneously active regional commitments.

## OSRM root cause, rather than a provider workaround

The unchanged public diagnostic, pinned OSRM image and freshly extracted/car-partitioned/customized
graph reproduce `code=Ok`, 69 negative distance cells, zero negative durations, zero invalid diagonals,
and maximum snapping distance 34.79285305 m. It fails before a matrix can enter optimization.
The PBF SHA-256 remains `4f36f7e749aef6f21128288ab8ca6447666a506a440d74db778a079974a71b49`.

The enhanced `tests/routing/probe_regional_matrix.py` decodes the pinned x86_64 v6.0 PhantomNode
hint layout **only for public-fixture diagnosis**, not as an application protocol. All 69 negative
cells have the same enabled edge-based node and equal integer source/target weights, with a
negative difference in floating-point distance offsets. This distinguishes the failure from
an HTTP decoding, matrix indexing, rounding, cache or graph/profile identity error in our provider.
The graph was rebuilt from the same input, not reused from a different OSRM version/profile.

For public fixture indices 4 -> 20, only the forward road direction is enabled. Source and target
forward weight+offset are both 335; their distance+offset values are approximately 139.083303 m
and 138.888037 m. The downstream-to-upstream subtraction is approximately -0.195267 m. The table
returns -0.2 m with duration 0. The Route API returns +0.2 m, duration 0 and weight 0, rather than
establishing a legal directional journey around the network. It is therefore not a verified
replacement for that cell. A legitimate loop might require a much larger journey; no such distance
is inferred or manufactured here.

The exact upstream [v6.0.0 implementation](https://github.com/Project-OSRM/osrm-backend/blob/v6.0.0/src/engine/routing_algorithms/many_to_many_mld.cpp)
combines phantom weights and accepts nonnegative weight, while independently combining distance
offsets. [Integer weight/duration versus floating distance types](https://github.com/Project-OSRM/osrm-backend/blob/v6.0.0/include/util/typedefs.hpp)
permit a zero rounded weight without a nonnegative physical distance. This matches the maintainer's
[quantization analysis in issue 5855](https://github.com/Project-OSRM/osrm-backend/issues/5855) and
[loop-forcing work in issue 6852](https://github.com/Project-OSRM/osrm-backend/issues/6852).
The tested v6.0.0 source commit is `054b0a6395f689a47a908b26b12e32ed7704c533`.
These upstream links provide context; the raw fixture/hint observations above were reproduced locally.

The native regression intentionally asserts the pinned defect and strict rejection, so an upstream
upgrade requires reviewing that evidence rather than silently deleting the safeguard. A separate
test rejects the bad request, then uses the **same live provider** for valid roads and validates
proposals from both native engines. That demonstrates recovery on valid output; it does not claim
the bad coordinates were corrected. Existing graph/profile identity, directional durations,
maximum-snap, null reachability, payload limits, timeout and logging safeguards are unchanged.

## Commercial corrections and authority boundaries

Changed implementation files are `src/tirodhan/modules/routing_operations/admission.py`,
`admission_jobs.py`, `orchestration_models.py` and migration
`migrations/versions/0029_admission_retry_schedule.py`. Regression/measurement files are
`tests/integration/test_admission_reliability.py`, `test_commercial_admission_benchmark.py`,
`tests/routing/test_admission_insertion.py`, `test_osrm_quantization.py`, `test_matrix.py`,
`probe_regional_matrix.py` and the local `run-regional.ps1` wrapper. This report, its aggregate
JSON, the project/architecture/domain/schema/ER/idempotency documents and ADR-004 record the
implemented boundary. The OSRM provider, native adapters, payment/refund processor, route
optimizer, authoritative assignment/execution and production infrastructure files are unchanged.

`admission.py` now uses bounded **new-obligation insertion** into a prior certificate. It retains
every existing route subsequence, mandatory household, protected prefix and onboard discharge.
It inserts both household pickup and paired discharge, trying eligible committed resources first.
Every candidate passes the full independent feasibility gate, including all previous obligations.
The first feasible certificate is sufficient: this is capacity certification, not authoritative
fleet-cost optimization. Exhaustion/invalid seeds still fall back to both approved native engines.
No accepted obligation becomes optional to improve benchmark numbers.

Checkout replaces a passive reservation's projected IDs with real booking IDs. The fast path
recognizes only relational reservation -> booking aliases and then revalidates all live terms.
It does not trust the old certificate after binding, cancellation, amendments or source changes.
The latest-generation lookup uses the existing `(region_id, region_version)` unique index;
EXPLAIN confirmed an `Index Scan Backward using uq_region_capacity_version`.
Known working-set overflow is rejected before road acquisition/native execution, without truncation.

`admission_jobs.py` reserves one RUNNING preparation intent per region in a short PostgreSQL
transaction. A partial unique index provides the final owner exclusion. A regional row lock and
fresh owner check arbitrate overlapping claimers; other regions skip occupied ownership and can
prepare concurrently. An expired owner is reclaimed using the same intent and a new lease token.
No transaction or database connection is held across roads/native solving. This serializes only
regional certificate preparation, not customer enqueue, independent regions, checkout, payments,
route optimization workers or physical execution. Direct/internal reservation calls still face
the original capacity CAS/source fence; the claim is not a substitute for that financial/physical
correctness boundary.

Provider failures, whole-work deadlines and transaction/source conflicts remain PENDING with
durable exponential backoff (default 5 seconds, bounded at 60) until intent expiry. Backoff is
technical retry pacing, not a promise of service. It prevents hot loops and avoids turning the
third concurrent transaction conflict into an arbitrary terminal customer failure. Elapsed slots
are terminal `SLOT_UNAVAILABLE`, never a fabricated capacity success. Crash after certificate
commit replays that certificate, including historical success after intent expiry; existing
expiry checks still prohibit a fresh checkout bind using expired permission.

Payment-first cancellation, accepted booking/financial terms, canonical serviceability locations,
pickup windows, operating shifts, service minima, physical capacities, breaks, discharge/custody,
lease fences, customer authorization, idempotency and transactional source checks remain intact.
No payment/refund processor, dispatch optimizer, route assignment or custody mutation is changed.

## Migration and deployment compatibility

Migration 0029 adds nullable historical `region_id` (real FK), `next_attempt_at`, its due index and
the partial RUNNING-owner unique index to `capacity_admission_request`. Existing mappable intents
are backfilled through their original context/reattempt booking. Unmappable historical records
remain nullable; a worker resolves later-available membership before claiming, or fails closed
without a certificate if membership is still missing. New admissions resolve membership before
queuing. Nullable legacy IDs cannot become a bypass around the regional owner protocol.

Pause/drain old admission workers before a coordinated application/migration upgrade. If the old
deployment has multiple RUNNING intents in a region, upgrade refuses atomically, preserving those
owners rather than silently revoking them. Drain/finish/recover them with the old worker, then retry
the migration. Both refusal and populated downgrade/upgrade are tested. Downgrade removes only
the new preparation scheduling/membership fields/indexes; certificates, intent identities,
financial facts and operational history survive. It makes delayed PENDING work immediately due.
Do not mix old workers with the new ownership protocol during cutover.

## Benchmark method and interpretation

The opt-in `tests/integration/test_commercial_admission_benchmark.py` invokes the same domain
services as the customer API: authorized/idempotent admission submission, actual durable worker,
real OSRM acquisition, pinned VROOM/OR-Tools, transactional capacity reservation, real checkout with
required certificate binding, initial test-provider attempt and authenticated financial success
processor. **No accepted booking or certificate is bulk seeded.** Only synthetic identity/roles,
already resolved public-fixture serviceability and declared future operating inputs are fixtures.
External geocoding, paid payment calls, network/APIM/JWT middleware and production pricing are not
part of the measurement. The existing fixed test pricing/provider ports avoid external side effects.

Each workload has 20 identical declared future vehicles (10 kg/10 L) for eight hours. Bookings
use 64 public OSM road vertices, 1–2.5 kg / 1–2 L household declarations, configured 180-second
pickup and 60-second discharge minima. Seventy percent concentrate in the first two half-hour
slots, with the remainder distributed across the next four hours. All slots are real Delhi
business-time slots on the following service day, with explicit facility opening windows.
Future capacity does not require an active rider now; this preserves the existing admission model.

Application/native container: 2 CPUs / 4 GiB. Disposable PostgreSQL/PostGIS 17-3.5: 2 CPUs / 1 GiB.
OSRM **6.0.0**, VROOM **1.15.0**, OR-Tools **9.14.6206**; existing immutable image pins are retained.
The base runs use a Git archive of the exact base SHA with the same new test harness copied in.
All measurements are local clipped-map synthetic results on a shared desktop, not production SLOs.
Some correctness/static checks ran in separate containers during the campaign; hardware isolation,
representative full-city road profiles and repeated steady-state campaigns are not established.

The 48-customer and 2,000-intent cases use the compatibility `COMMITTED_FLEET` policy source with
regional admission enabled; all capacity evaluation is scoped to the actual OperatingRegion.
A separate matched 24-customer pair adopts the `REGION:<id>` control on both base and final code,
verifying the replacement policy-loading path with identical capacity/service settings. These
configuration modes are labeled explicitly in the JSON; they must not be conflated in comparisons.

P50/P95 use nearest rank over individual customer requests/attempts in each **single closed-burst
campaign**, not percentiles of repeated campaign totals. Decision latency includes queueing from
submission to observed terminal preparation. Attempt latency measures worker preparation; checkout/
test capture is reported separately. Completion throughput includes denials, so successful purchase
throughput must also be considered. Pending work is censored, never counted as completed success.

| 48-customer campaign | Total | Accepted | Technical failures | Decisions/s | Decision P50 / P95 | Attempt P50 / P95 |
|---|---:|---:|---:|---:|---:|---:|
| Base, one worker | 163.46 s | 48 | 0 | 0.294 | 40.85 / 146.98 s | 3.47 / 6.60 s |
| Base, four workers | 179.16 s | 32 | 16 | 0.268 | 55.44 / 171.59 s | 4.04 / 12.17 s |
| Insertion/backoff only, one worker | 32.81 s | 48 | 0 | 1.463 | 9.96 / 26.55 s | 0.49 / 1.49 s |
| Insertion/backoff only, four workers | 285.16 s | 48 | 0 | 0.168 | 25.80 / 208.69 s | 1.01 / 3.07 s |
| Final regional owner, four workers | 42.52 s | 48 | 0 | 1.129 | 13.93 / 35.13 s | 0.62 / 1.83 s |
| Final, four workers, interleaved checkout/capture | 45.32 s | 48 | 0 | 1.059 | 17.84 / 41.95 s | 0.59 / 1.36 s |

The base four-worker failures were six stale-source and ten SERIALIZABLE transaction retry
exhaustions (33.3% of intents). The final four-worker case had none. These observations motivated
regional ownership; retries alone were slower despite eventually completing all 48.
The final normal case executed 9,604 SQL statements with 14.15 seconds of aggregate statement wall
time (including waits), versus 10,524 / 111.65 seconds for the base four-worker case. Maximum
individual statement time was 0.476 versus 4.544 seconds. This does not equate SQL wall time with
database CPU. Read-only session rollbacks in statistics are not failed customer transactions.

The matched **adopted regional-policy** case (24 customers, four workers) measured:

| Code | Total | Accepted / failed | Decisions/s | Decision P50 / P95 | Attempt P50 / P95 |
|---|---:|---:|---:|---:|---:|
| Exact base | 50.87 s | 17 / 7 | 0.472 | 14.95 / 47.13 s | 2.67 / 5.77 s |
| Final | 12.08 s | 24 / 0 | 1.987 | 5.07 / 8.79 s | 0.30 / 0.45 s |

Base failures were two stale-source and five transaction-retry exhaustions (29.2%). Successful
purchase throughput was 0.334/s before and 1.987/s after. Aggregate SQL wall time fell from
56.32 to 4.48 seconds; maximum statement wall time from 3.312 to 0.033 seconds. This pair verifies
the current adopted region configuration rather than inferring it from the compatibility runs.
It is still one synthetic burst per revision, not a production throughput guarantee.

Machine-readable aggregates, resource measurements and the bounded 2,000-intent stress outcome
are retained in `docs/benchmarks/ROUTING_RELIABILITY_ADMISSION_RESULTS.json`.

The bounded 2,000-intent case submitted **all 2,000 through the real admission service**, allowed
120 seconds for certificate processing, and completed 67 checkouts/test captures. At the end there
were **67 SUCCEEDED and 1,933 PENDING**, no terminal failures and no duplicate/lost accepted booking
or certificate. Total measured workflow time was 199.88 seconds, including submission and checkout;
fixture preparation is excluded. Completed purchase throughput was 0.335/s. Enqueue P50/P95 was
0.0246/0.0511 s; preparation-attempt P50/P95 was 1.44/5.42 s. Observed decision P50/P95 among the
**67 completed intents only** was 92.55/159.53 s. This is heavily censored and is not the latency
distribution of all 2,000 customers. Pending intents remained eligible for later finite work.
The run did not reach the 128-active-certificate boundary within its budget; that additional bound
is established by code and the explicit overflow regression, not by pretending the stress finished.

PostgreSQL sampling recorded baseline single-worker peaks of **16.68% CPU / 88.82 MiB** over 74
samples, and **30.11% / 160.5 MiB** over 344 corrected-campaign samples. Docker CPU 100% represents
one logical CPU; the database quota was two. Baseline sampling covers part of its single-worker
campaign, whereas the latter includes multiple campaigns, fixtures, idle and the 2,000-intent case.
These are descriptive sampled maxima, **not a controlled before/after resource ratio or reliable
peak sizing**. Approximately three-second sampling can miss bursts. All recorded benchmark database
snapshots had zero deadlocks and zero temporary bytes. The `pg_stat_database` counters in the JSON
are cumulative within each disposable database; they are not mislabeled as per-run deltas.

Under the 2,000-intent workload, EXPLAIN ANALYZE of the occupied-region admission query used a hash
anti join and the RUNNING-owner index, completing in **2.169 ms with 61 shared-buffer hits** and no
temporary spill. It was not a demonstrated query bottleneck. Native/certificate computation and
sequential regional promise creation remain material limits. Client-side SQL wall measurements
include waits and event-loop scheduling; their maximum is not a database execution-time diagnosis.

## Validation and remaining production work

Actual final validation:

- Python 3.13 broader native environment: **737 passed, 1 failed, 6 warnings in 485.69 s**. This covers all
  unit/routing tests, both native engines, new admission tests, existing operational orchestration,
  regional planning/migration, collection/payment and payment-preparation regressions, with
  Hypothesis seed **81017**. The failure is the existing
  `test_timeout_traceback_and_http_logging_hide_sensitive_url`. It also fails on an isolated archive
  of the exact unchanged base in Python 3.13 (15.05 s), and passes in the default Python 3.12 image
  (14.97 s). No OTP implementation/test is changed; this run is **not all green**. The broader run
  preceded the final nullable-legacy claim safeguard; the focused checks rerun that affected boundary.
- Final default Python 3.12 focused suite: **37 passed, 2 optional-native skips, 1 warning in 48.82 s**.
- Final native focused run after the legacy compatibility safeguard: **39 passed, 1 existing
  SQL warning in 52.17 s**, including both native engines and the migration-refusal regression.
- Nine completed benchmark cases passed, including the intentionally budget-limited stress case.
  These include three exact-base cases, two insertion/backoff comparisons and four final cases.
- Ruff lint/format, mypy (203 source files), dependency consistency and PowerShell syntax checks
  passed; affected-code checks cover the last source refinements. The full repository suite was
  not rerun; the explicitly listed relevant suite above was run.
- Populated migrations round-trip financial/intent/certificate facts, refuse overlapping legacy
  owners and retain all three financial initiation-term triggers. Strict metadata comparison keeps
  the two known application-index differences, excludes catalog-owned PostGIS objects and reports
  **no new application drift**. CI-equivalent PostGIS/topology/TIGER extensions were provisioned in
  the fresh disposable validation databases.

Warnings include the unchanged one-row reservation/booking geography cross-product query warning,
FCM deprecations and the existing Python 3.13 ByteStream cleanup warning. The geography predicates
constrain both primary keys; no unbounded cross-product performance claim is made or unrelated
query changed. The Python 3.13 OTP failure remains separate existing runtime/test debt if that
runtime is selected; these corrections do not claim to close it.

The new regression set covers provider rejection/timeouts/backoff/recovery, committed response
loss and lease takeover/expiry, repeated conflicts, actual two-session competing capacity,
independent regions, DB unique owner enforcement, cancellation during road acquisition, paid
booking certificate alias reuse, elapsed slots, early technical overflow, populated migration
round trips/refusal and both native engines after a rejected matrix request.

**Outstanding OSRM work:** qualify a verified upstream fix/release or maintain a narrowly reviewed
backport addressing phantom ordering and loop forcing in table and route search. Increasing profile
precision, switching algorithms or replacing cells with Route results is not established as a
general correction. Estimate 3–5 engineering days for a targeted fix/backport and regression work,
plus 2–3 days for car/bicycle and representative city-graph qualification, assuming the defect's
scope remains as observed. Upstream release timing is outside this estimate. Until then, valid
geographic input can still fail closed repeatedly; unrestricted V2 production routing is blocked.

**Outstanding high-volume admission work:** the certificate still validates one bounded regional
day/horizon, retaining all accepted commitments and active holds. It has a configured maximum of
128 requests and dense matrices/native bounds. Removing that check would be unsafe, and simply
raising it is not a demonstrated scalability solution. A larger release needs dependency-fenced
incremental per-resource certificates/affected-route evaluation (including cross-slot vehicle
commitments and shared facilities), with consistent region-wide promise accounting and selective
native repair when inputs change. This fits PostgreSQL and the existing engines, but requires
more than a bounded search optimization. Estimate 5–8 engineering days for implementation and
another 3–5 for adversarial concurrency/load qualification; refine against the intended regional
peak and hardware. No new service, database or paid dependency is intrinsically required.

The critical path is shorter for bounded commercial workloads: unnecessary reoptimization,
same-region solve collisions and terminal transient failures are corrected. **Unrestricted V2
launch remains blocked by the OSRM defect and by high-volume regional certification if the
required launch volume exceeds the demonstrated bound.** This report does not reopen regional
administration, outcome retention, routing objectives or deployment architecture, and implements
no subsequent increment.

## Reproduction

Use disposable PostgreSQL only: integration fixtures truncate application tables. Existing local
images/containers suffice; no production service or paid provider is used. The PowerShell harness
creates a uniquely named disposable database/graph volume and cleans up its own resources.

```powershell
# Existing harness now includes the focused new regressions and pinned native/profile checks.
powershell -NoProfile -File tests/routing/run-regional.ps1 -SkipBuild
# Omit -SkipBuild to rebuild the existing pinned local test images.
powershell -NoProfile -File tests/routing/run-regional.ps1 -SkipBuild -CommercialAdmission -AdmissionCount 48 -AdmissionConcurrency 4
powershell -NoProfile -File tests/routing/run-regional.ps1 -SkipBuild -CommercialAdmission -AdmissionCount 48 -AdmissionConcurrency 4 -InterleavedCheckout
powershell -NoProfile -File tests/routing/run-regional.ps1 -SkipBuild -CommercialAdmission -AdmissionCount 24 -AdmissionConcurrency 4 -AdoptedPolicy
powershell -NoProfile -File tests/routing/run-regional.ps1 -SkipBuild -CommercialAdmission -AdmissionCount 2000 -AdmissionConcurrency 4 -AdmissionBudget 120
```

The campaign used the equivalent direct Docker invocations with a reused task-owned graph volume
and disposable databases; the revised all-in-one PowerShell wrapper received a syntax check rather
than a redundant full campaign. Exact runner: `python tests/routing/regional_entry.py -q -s
tests/integration/test_commercial_admission_benchmark.py -p no:cacheprovider`, with
`TIRODHAN_ADMISSION_BENCHMARK=1`, `TIRODHAN_ADMISSION_COUNT`, `TIRODHAN_ADMISSION_CONCURRENCY`,
`TIRODHAN_ADMISSION_BUDGET` and optional `TIRODHAN_ADMISSION_INTERLEAVED=1` /
`TIRODHAN_ADMISSION_ADOPTED=1`. Mount the public graphs at
`/graphs`, set the disposable `TIRODHAN_TEST_DATABASE_URL`, and use the existing increment-8 native
image. The supervisor owns/reaps private OSRM. The unchanged-base cases used the base archive plus
this benchmark, with immediate finite worker consumption; final backoff cases also wait for due
retries within the explicit budget. The one-worker comparison is insertion/backoff evidence; the
final four-worker comparison includes the regional owner.

Focused explicit command inside that environment:

```text
python -m pytest -q tests/integration/test_admission_reliability.py tests/routing/test_admission_insertion.py tests/routing/test_matrix.py tests/routing/test_osrm_quantization.py -p no:cacheprovider
```

For native tests also set `TIRODHAN_REGIONAL_GRAPH_DIR=/graphs` and
`TIRODHAN_ROUTING_ENGINE_TESTS=1`. The raw public diagnostic is
`python tests/routing/probe_regional_matrix.py /graphs/car/map.osrm` in the pinned image.
