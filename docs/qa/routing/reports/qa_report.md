# Tirodhan V2 Routing - Autonomous QA Execution Report

## 1. Execution Context
- **Trigger SHA:** `b281e03a47d202fd8b5c3d895075aa358062c5fa`
- **Actual Implementation SHA:** `b281e03a47d202fd8b5c3d895075aa358062c5fa`
- No SHA mismatch. Executing against trigger SHA.
- **Execution Environment:** Autonomous execution sandbox. (Docker Operations Blocked)

## 2. Environmental Limitations & Blocked Coverage
The automated execution environment strictly blocks Docker operations due to underlying container engine constraints (`overlayfs` mount failures in unprivileged mode).

Consequently, real native routing infrastructure (OSRM, VROOM, PostgreSQL/PostGIS databases via containers) cannot be provisioned or started.

**Blocked Test Coverage (Not Executed, Explicitly Failing or Skipped):**
1. **End-to-end Operational Simulation:** Operational simulation scripts (e.g. `tests/routing/run-operational.ps1`, `run-regional.ps1`, `run-native.ps1`) require Docker to spin up PostGIS and OSRM components. These could not be run, and no synthetic disposable PostGIS could be built. This prevents executing the full end-to-end flow with realistic geographical coordinates and concurrent operations in database tables.
2. **Native Dependency (OSRM/VROOM/OR-Tools) Execution:** Tests explicitly calling native solvers or real matrix evaluations failed/skipped when opted into, due to the unavailability of `osrm-routed` or local `vroom` instances and matrix URLs (`SOLVER_UNAVAILABLE`). E.g. native integration in `tests/routing/test_native_engines.py` and `tests/integration/test_fleet_hypothesis_native.py`.

## 3. Executed Tests & Findings
We successfully executed tests that did not require missing services or unmocked database operations.

1. **Existing Baseline Regressions:**
   - **Command:** `pytest tests/routing --tb=short`
   - **Result:** `85 passed, 28 skipped`
   - Non-native, mock-based logic, validation, and contract integrity checks pass successfully. Native tests accurately skip based on explicitly requested test environment variable toggles.

2. **Genuinely New Adversarial Testing:**
   - We authored a new adversarial, property-based verification module (`tests/routing/test_qa_adversarial.py`).
   - This test subjects the core `consolidate()` CP-SAT decision routine to adversarial problem costs ranging from -10,000 to +10,000, confirming that optimization feasibility holds even when route costs generate numerical boundary stress.
   - **Result:** The CP-SAT solver successfully handled these extremes, strictly preserving feasibility conditions and producing optimal outputs. `1 passed`.

### Categorized Findings

1. **Environment Failure: Missing Containerization Runtime**
   - **Type:** Environment failure.
   - **Description:** The automated QA environment does not support nested containerization or required overlay mounts. OSRM, VROOM, and PostGIS could not be run.
   - **Affected Business Rules:** Complete integration validation of native geographic planning, concurrent transactional database commits for routing, and end-to-end simulation workflows were entirely blocked.
   - **Remediation:** QA workflows requiring real integration containers must be run in an environment natively supporting Docker, or provision fixed ephemeral VMs instead of sandbox containers. The codebase natively degrades cleanly when these fail (e.g. raising `SOLVER_UNAVAILABLE` errors correctly when engines crash or are unreachable).

## 4. Next Steps
No changes to production code, infrastructure, or configuration were made, as per standing mandate. The codebase acts deterministically correctly within bounds we could test (e.g. `consolidate()`).
