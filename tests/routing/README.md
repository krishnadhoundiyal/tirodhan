# Reproducing native routing tests

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
