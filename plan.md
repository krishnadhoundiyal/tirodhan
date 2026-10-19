1. **Analyze Requirements and Scope:**
   - Implement an operational geographical rolling QA process using real routing engines (VROOM, OR-Tools).
   - Configure 4 logical regions: West Delhi, North Delhi, South Delhi, East Delhi with distinct booking distributions.
   - Run operational planning for rolling intervals and check accumulations (20, 40, 60 boundaries) and horizons (2, 3, 4 hours).
   - Use `Hypothesis` for stateful property-based testing.
   - Generate before-and-after snapshot reports.
   - Record genuine VROOM, OR-Tools, and CP-SAT execution traces.
   - Write output artifacts to `docs/qa/routing/reports/`.
2. **Review Codebase Execution paths (`src/tirodhan/modules/routing_operations/regional_planner.py`):**
   - The entry point for the actual executor is `run_region(...)`.
   - Setup configuration via `configure_region(...)` creating the `OperatingRegion` and `RegionalPolicy`.
   - Fleet wakeup trigger is handled by `regional_wake`.
3. **Environment Setup & Tooling:**
   - Install required dependencies like `pytest`, `hypothesis`, and the local project `[routing,dev]` via pip since it's required for real engine tests (`TIRODHAN_ROUTING_ENGINE_TESTS=1`).
4. **Implement QA Report Writer Module:**
   - Create a module to structure, format, and save the 11 required artifacts (`QA_EXECUTION_CONTRACT.md`, `QA_SCENARIO_MATRIX.md`, `QA_BOOKINGS.csv`, `QA_EXECUTIONS.json`, `QA_SOLVER_TRACE.json`, `QA_PLAN_SNAPSHOTS.json`, `QA_ASSIGNMENTS.csv`, `QA_INVARIANTS.json`, `QA_METRICS.json`, `QA_TEAM_REPORT.html`, `QA_FINAL_REPORT.md`).
5. **Implement the Generator / Stateful Driver using Hypothesis:**
   - Model the regions.
   - Define realistic geometries (pickup/drop off) in West, North, South, East Delhi using test utility `RoadNode` geometries from `tests/fleet_verification/strategies.py` or synthetic ones that snap to the OSM graph.
   - Step through the rolling timeline for each region (simulating 20, 40, 60 requests arriving, triggering `run_region`, tracking assignments).
   - Assert invariants explicitly: no silent loss of committed routes, no concurrent overlaps on a rider unless validly ordered.
6. **Implement Test Cases (`tests/qa/operational_rolling/test_operational_geographical_rolling_qa.py`):**
   - We need tests to cover the full matrix: 4 Regions x 3 Thresholds x 3 Horizons, plus Hypothesis stateful tests.
   - Set up disposable PostgreSQL (`TIRODHAN_TEST_DATABASE_URL`) via `run-operational.ps1` style execution or custom docker fixture inside our script.
7. **Complete pre commit steps:**
   - Complete pre commit steps to make sure proper testing, verifications, reviews and reflections are done.
8. **Finalizing and Submission:**
   - Verify artifacts are generated, log results in the PR or report, commit to `jules/operational-geographical-rolling-qa`, and submit.
