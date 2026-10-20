# Geographical operational rolling QA

Read [the final assessment](QA_FINAL_REPORT.md), then open
[the review index](review/index.html) or [the primary graphical report](review/primary/QA_TEAM_REPORT.html).
The HTML is self-contained: no external map tiles, JavaScript library or hosted service is needed.
Polylines show stop order schematically; measured costs use actual directed OSRM road matrices.

## Evidence layout

- [Execution contract](QA_EXECUTION_CONTRACT.md): inspected production entry points and authority boundaries.
- [Scenario matrix](QA_SCENARIO_MATRIX.md): declared synthetic geography, appointments, fleet and policy.
- [Rider route comparisons](review/route-reports/primary-10c4655/index.html): all four regions and R1–R4, with before/after route maps, full booking keys and recorded timings.
- [All scenario measurements](review/QA_ALL_SCENARIOS.json): 37 scenario summaries and actual call counts.
- [Archive manifest](review/ARCHIVE_SHA256.json): SHA256 and byte length of every complete scenario ZIP.
- `review/primary/`: immediately readable required outputs, derived diffs and publication audit.
- `review/<scenario>.zip`: complete raw responses, submitted CP models, proposals, relational snapshots,
  and the required eleven artifacts for each of the 37 geographical scenarios.
- `review/overlap.zip`, `stateful.zip`, `recovery.zip`, `lifecycle.zip`: additional actual runtime evidence.
- `native-property-results.xml` and `regression-results.xml`: pytest machine-readable results.

The raw working directories are ignored under `evidence/`; the verified ZIPs are versioned.
Extract a ZIP before opening its HTML. `raw/QA_METRICS.json` preserves the original recorder output.
The report's `QA_METRICS.json` uses independently revalidated current publication costs; immutable
revision models can retain completed stops and therefore have a different cost scope. Every
publication audit matches its recorded selection pool digest, validates the exact problem/proposal,
and compares pending stop order with committed relational authority. It does not rerun an optimizer.

## Reproduce

From the repository root, with Docker Desktop and PowerShell:

```powershell
# Build the existing pinned local test images, derive the broad road graph, run primary.
./tests/routing/run-geographical.ps1

# All 36 extended combinations plus primary; reuse the pinned image/derived graph.
./tests/routing/run-geographical.ps1 -SkipBuild -Extended

# Separate finite real-service acceptance scenarios, each in its own disposable database.
./tests/routing/run-geographical.ps1 -SkipBuild -Selection tests/integration/test_geographical_stateful.py
./tests/routing/run-geographical.ps1 -SkipBuild -Selection tests/integration/test_geographical_overlap.py
./tests/routing/run-geographical.ps1 -SkipBuild -Selection tests/integration/test_geographical_recovery.py
./tests/routing/run-geographical.ps1 -SkipBuild -Selection tests/integration/test_geographical_lifecycle.py

# Quick generated ledger checks, without containers.
.venv/Scripts/python.exe -m pytest -q tests/integration/test_geographical_evidence.py --hypothesis-seed=1729
```

Each wrapper invocation prints a fresh `.tools/geographical-qa/runs/<uuid>` output directory.
`-ArtifactsDirectory <repository-relative-path>` selects a different **empty** directory.
The wrapper refuses nonempty outputs so unrelated runtime traces cannot silently mix.
It creates and removes only its own uniquely named runtime and PostgreSQL/PostGIS containers.
Graph files are local derived fixtures; this neither deploys nor enables production dispatch.

The exact versioned PBF and hashes are described in
[the fixture provenance](../../../tests/routing/data/geographical-README.md).
Snap eligibility can be regenerated with `tests/integration/geographical_snap_fixture.py` against
that graph; `geographical_nodes.py` additionally needs `osmium==4.2.0` only to regenerate source
vertices. Fixture selection never uses solver outcomes or matrix cost filtering.

To package a newly completed raw run (Python environment with repository dependencies):

```powershell
$env:PYTHONPATH='src;tests/routing'
.venv/Scripts/python.exe tests/routing/package_geographical.py <raw-directory> <review-directory>
# Presentation-only refresh of existing complete archives, including strict publication replay.
.venv/Scripts/python.exe tests/routing/package_geographical.py <raw-directory> <review-directory> --refresh-reports
```

Only package complete successful geographical scenarios as acceptance evidence. Preserve failed
attempts separately and describe them explicitly. Reproduction is semantic, with seed 1729 and
retained exact inputs; fresh UUIDs, current dates, native scheduling and heuristic tie breaks need
not produce byte-identical routes or elapsed times.

For the default repository regression suite, start an independent disposable
`postgis/postgis:17-3.5`, set `TIRODHAN_TEST_DATABASE_URL` to its test database, and run
`python -m pytest -q tests -p no:cacheprovider --tb=short --hypothesis-seed=1729` in the existing
`tirodhan-routing-tests:increment-8` image with `PYTHONPATH=/repo/src:/repo/tests/integration`.
The expensive geographical/native opt-ins are separate. To execute the six existing native
oracle tests, set `TIRODHAN_ROUTING_ENGINE_TESTS=1` and run
`tests/integration/test_fleet_hypothesis_native.py` in that pinned image.

The explicit geographical opt-in fails with an actionable setup error when its DB, OSRM endpoint,
output directory or pinned native binaries are unavailable. Default collection intentionally skips
the expensive acceptance cases; a skip is never reported as geographical success.
