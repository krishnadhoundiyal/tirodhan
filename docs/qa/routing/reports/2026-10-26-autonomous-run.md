# Tirodhan Routing QA Report
**Date:** 2026-10-26

## Version Information
* **Triggering SHA:** `b281e03a47d202fd8b5c3d895075aa358062c5fa`
* **Actual Implementation SHA:** `b281e03a47d202fd8b5c3d895075aa358062c5fa`
* **SHA Mismatch:** No mismatch detected. `main` has not advanced since the trigger.

## Execution Environment Constraints
* **Environment:** Jules isolated sandbox (Python 3.12.13).
* **Limitations:** Native OSRM, Docker (`docker run` failed due to overlay filesystem limitations), and PostGIS are **explicitly blocked** in this automated execution environment.
* **Blocked Coverage:** Due to the infrastructure blocks, Native E2E operational simulation, Geographical QA, OSRM matrix tests, adversarial routing combinations, and native VROOM/OR-Tools tasks cannot be run.

## Test Execution Summary
The non-native regression suite was executed via `python -m pytest -q tests -p no:cacheprovider`:
* **Command:** `python -m pytest -q tests -p no:cacheprovider`
* **Result:** `697 passed, 1009 skipped, 4 warnings in 25.48s`
* **Note:** The skipped tests directly correspond to dependencies on `TIRODHAN_TEST_DATABASE_URL` (PostgreSQL/PostGIS) and native engines (OSRM, VROOM, OR-Tools).

## Findings
1. **Blocked E2E and Adversarial Simulation (Environment Failure - P1)**
   * **Category:** Environment failure.
   * **Details:** The autonomous QA mandate requests executing End-to-End operational simulations with synthetic paid bookings on a disposable PostgreSQL/PostGIS database and native routing solvers. Due to the sandbox environment restrictions blocking Docker, this coverage is entirely blocked.
   * **Affected Business Rules:** The full pipeline of OSRM -> VROOM/OR-Tools -> CP-SAT -> Fenced database activation cannot be validated in this run.
   * **Remediation & Retest Criteria:** Infrastructure updates are required to support disposable Docker-based PostGIS and OSRM containers within the sandbox execution environment to run `tests/routing/run-regional.ps1` and `tests/routing/run-operational.ps1`. Once enabled, the QA run should be re-triggered.
