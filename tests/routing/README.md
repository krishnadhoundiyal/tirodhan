# Reproducing native routing tests

Increment 7 extends `run-operational.ps1` with regional admission, custody/reattempt,
preflight and migration coverage. `-Benchmark` explicitly adds the real native/PostgreSQL
and synthetic-route batching measurements; `-Extended` selects the expensive Hypothesis
profile. `-Seed` overrides the reproducible default 61010. Both modes are opt-in and use
only the same disposable resources. OSRM permits 512-node tables and suppresses request
URL logging. Keep `-Extended` out of regular CI.

```powershell
powershell -NoProfile -File tests/routing/run-operational.ps1 -SkipBuild -Benchmark -Seed 71015
```

Use `-SkipBuild` only with the current dependency image already available. Benchmark output
is labelled `INC7_PG_BENCHMARK`/`INC7_BATCH_BENCHMARK`; the latter deliberately uses synthetic
routing to isolate PostgreSQL command/batching costs. Full repository validation uses the
default dependency environment and a separate disposable PostGIS database. Recorded results
and actual solver/road limitations are in `docs/V2_ROUTING_INCREMENT_7.md`.

Increment 3 operational reproduction (disposable PostgreSQL/PostGIS, the same pinned
road/solver engines, and actual booking/ownership/custody projections):

```powershell
powershell -NoProfile -File tests/routing/run-operational.ps1
```

Use `-SkipBuild` only when the operational test image is already built with the current
dependency manifest. The runner mounts the current source read-only, validates the public
road fixture hash, creates its own database/server/volume with no host ports, and removes
them in `finally`. Default native tests still require no database, using the `routing-tests`
Dockerfile target; operational tests use `operational-tests`. The optional image resolves
the complete routing/development dependency set; it is not a production API image.

From the repository root on PowerShell with local Docker:

```powershell
powershell -NoProfile -File tests/routing/run-native.ps1
```

The script builds the local-only test image, generates an OSRM MLD graph from the checked
public OSM fixture, runs OSRM and then all tests in this directory. The runner shares
OSRM's network namespace to use loopback HTTP, with no exposed port. UUID names avoid
collisions. Containers and the temporary graph volume are removed even on failure; the
test image remains cached locally. It requires network access for image/package downloads
on the first build, then map tests require no external road service. Container CPU/memory
limits are 2 CPU/2 GiB for graph/server, 2 CPU/4 GiB for tests. This script provisions
no cloud resources and is deliberately separate from default PR checks/deployment.

Pins:

- OSRM 6.0.0:
  `ghcr.io/project-osrm/osrm-backend@sha256:729461bcc9ae9e6aafa92c0f93db9b060a32e85d5e72092c01ae4a4a9f1eb564`.
- VROOM 1.15.0:
  `ghcr.io/vroom-project/vroom-docker@sha256:247d5683d6745c755d718a156d16b16aac80baccc276a003a68b986c13883b08`.
- OR-Tools 9.14.6206; pytest 8.4.2; pytest-asyncio 0.26.0; httpx 0.28.1.
- Road fixture checksum/license/source: [data provenance](data/README.md).

Ordinary `pytest` runs mocked acquisition and real child-process failure tests and
explicitly skips real-engine tests. Native opt-in requires
`TIRODHAN_ROUTING_ENGINE_TESTS=1`, installed pins, and for the two real-road tests
`TIRODHAN_OSRM_TEST_URL` and `TIRODHAN_OSRM_GRAPH_VERSION` (set by the script). The
optional `pip install '.[routing]'` installs OR-Tools only; it does not install VROOM
or provision OSRM. Full repository integration tests separately require disposable
PostgreSQL/PostGIS via `TIRODHAN_TEST_DATABASE_URL`; never point them at operational data.

The real-road tests print per-engine process/search elapsed seconds and independently
reconstructed travel seconds/distance. They use synthetic demand, facility hours and
capacities on a clipped car-profile graph. Results prove the acquisition-to-solver-to-
validator path, not loaded-vehicle calibration, production performance or route optimality.
# Increment 4 operational authority coverage

The operational runner now also executes tests/integration/test_routing_dispatch.py,
using image tag tirodhan-routing-tests:increment-4. Both real engines activate committed
routes, collect into a vehicle, revise the loaded BUSY vehicle for an additional booking,
discharge with cargo/receiving/photo proof and complete the requests through existing
evidence rules. Disposable PostgreSQL migrations, concurrent activation/cancellation,
historical recovery and downgrade guards run alongside the existing native suite.
The exact reviewed source is mounted read-only even with -SkipBuild; that option reuses
only a dependency image with the unchanged dependency manifest.

## Increment 5 continuous fleet coverage

The operational runner uses `tirodhan-routing-tests:increment-5` and also executes
`test_routing_fleet.py` and `test_routing_fleet_reliability.py`. The dependency pins above
are unchanged. It retains Increment 4's complete collect/revise/discharge tests.
New real-road tests execute both native engines, append positions while they solve,
rebase/activate, collect and revise with actual vehicle cargo and additional demand.
They also exercise either native engine unavailable, using the other's validated result.
Deterministic directed-matrix regressions cover economic baselines, whole-block
ownership transfer, protected departure, expired owners, crash boundaries, concurrent
cycles, publication rebase, device authorization, unsafe-resource holds and backlog
progress. Migration tests check populated prior-history round trips, catalog-filtered
application drift, immutable history and downgrade refusal after adoption.
The tests use synthetic capacity, coefficients and demand; road measurements are
reproducible fixture observations, not production cost or scale claims. See
[Increment 5 report](../../docs/V2_ROUTING_INCREMENT_5.md).

## Increment 6 generated verification

The operational image/runner is now `tirodhan-routing-tests:increment-6`, with pinned
test-only Hypothesis 6.168.5. Build this dependency image before using `-SkipBuild`.
The runner includes the independent exhaustive oracle, native differential cases,
database-backed rule machine and all 14 PostgreSQL race families with seed 61010.
The parent OSRM/VROOM/OR-Tools pins and public road fixture remain unchanged.

Ordinary repository CI includes bounded generation and skips the four native-only
generated/regression tests. Set `TIRODHAN_HYPOTHESIS_PROFILE=fleet-extended` for longer
campaigns; history/race suites reset application data and must run serially on one
disposable database. Separate databases permit parallel invocations. Use
`--hypothesis-seed=<integer>` and `--hypothesis-show-statistics` for replay/evidence.
On read-only mounts, set `TIRODHAN_HYPOTHESIS_DATABASE=/tmp/hypothesis`.
No business-infeasible inputs are filtered away; expected rejection is tested.
See [Increment 6](../../docs/V2_ROUTING_INCREMENT_6.md) for concrete commands, scope,
measured economic counterexample, production correction and verification limits.
