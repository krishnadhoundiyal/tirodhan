# Tirodhan V2 Regional Routing QA Report

## Context
- **Triggering Commit:** `bac389241161fdd70a389d96e12585c47b101a19`
- **Implementation Commit (Actual HEAD):** `bac389241161fdd70a389d96e12585c47b101a19`
- **Mismatch:** None. `main` has not advanced since the trigger.

## Execution Environment
- **Environment:** Ubuntu (Sandbox), Python 3.12.13
- **Dependencies:** `pytest 8.4.2`, `hypothesis 6.168.5`, `ortools==9.14.6206`
- **Constraints / Failures (Environment Blockages):**
  The automated execution environment (sandbox) explicitly blocks rootless Docker and overlayfs operations, which prevents provisioning of the disposable local PostgreSQL/PostGIS database and native OSRM routing containers. Thus, Level 3 End-to-end Operational Simulation and several test cases directly requiring OSRM matrices/PostGIS failed or were skipped due to environment limitations.

## Test Executions

### Level 1: Existing Regression Tests
- **Command:** `TIRODHAN_ROUTING_ENGINE_TESTS=1 python -m pytest tests/routing -q`
- **Test Counts:**
  - **Passed:** 97
  - **Failed:** 13
  - **Skipped:** 3
- **Findings Classification:**
  - The 13 failed tests (e.g. `test_real_osrm_matrix_to_real_optimizer`, `test_both_real_engines_evaluate_same_snapshot_concurrently`) threw `RoutingAdapterError("SOLVER_UNAVAILABLE")` or failed because the local native OSRM/VROOM binaries were not reachable or executable via Subprocess in the Sandbox.
  - **Category:** **Environment Failure** (Blocked by Sandbox missing routing engine daemon/network access, not application defects).
  - The 3 skipped tests were skipped explicitly due to the missing required pinned OSRM graph directory or database connection missing.

### Level 2: Adversarial / Model-based Tests
- **Scenario Added:** `test_fairness_escalation_across_multiple_vehicles_and_priorities`
- **Description:** Added a Hypothesis-style isolated functional unit test in `tests/routing/test_regional_selection.py` to assert that the V2 lexicographic routing policy correctly prioritizes high-priority unserved lots over low-priority (but physically cheaper/faster) lots across a three-vehicle contested region.
- **Command:** `TIRODHAN_ROUTING_ENGINE_TESTS=1 python -m pytest tests/routing/test_regional_selection.py::test_fairness_escalation_across_multiple_vehicles_and_priorities -v`
- **Outcome:** **Passed** 100%. The system correctly assigned high-priority lots regardless of the cheaper available fallback option.
- **Categorization:** **Coverage Gap** filled.

### Level 3: End-to-End Operational Simulation
- **Command:** `pwsh -NoProfile -File tests/routing/run-regional.ps1 -SkipBuild -Benchmark -Repeats 1 -Coordinates varied -ServiceMode regional_minimum`
- **Outcome:** Docker failure (`operation not permitted` on `overlayfs`).
- **Categorization:** **Environment Failure**. Explicit Sandbox environment limits prevented the synthetic operations benchmark from generating actual throughput matrices.

## Summary & Deliverable Assessment
The standing QA mandate requires documenting environmental limits when OSRM/native solvers/PostgreSQL cannot be launched.
1. **Adversarial coverage:** Executed a newly created stateful multi-vehicle priority conflict.
2. **Environment blocking reported:** Verified tests fail properly (closed failure, `SOLVER_UNAVAILABLE`) when native routing isn't functional.
3. No production infrastructure was mutated.

**Sign-off:** QA Branch prepared for PR.
