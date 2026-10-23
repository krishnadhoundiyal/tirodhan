# Independent QA Routing Execution Report

**Target Commit SHA:** `b281e03a47d202fd8b5c3d895075aa358062c5fa`
**Actual Evaluated SHA:** `b281e03a47d202fd8b5c3d895075aa358062c5fa`

## Execution Environment

- **OS:** Ubuntu Linux (sandbox environment)
- **Python:** 3.12.13
- **Dependencies:** `pytest 8.4.2`, `hypothesis 6.168.5`, `asyncpg`, `sqlalchemy`, etc.

## 1. Environment Limitations / Exclusions

The current execution environment blocks native Docker operations (`docker run` failed with `failed to mount /tmp/containerd-mount... invalid argument`). This acts as an explicit blocker for several QA requirements:
- **Database Simulation:** Cannot provision disposable PostgreSQL/PostGIS database. Consequently, integration test suites (`test_compaction_execution.py`, `test_fleet_hypothesis_stateful.py`, `test_fleet_hypothesis_concurrency.py`) requiring DB connections are severely restricted or skipped.
- **Native Routing Integration:** Cannot provision OSRM/VROOM/OR-Tools instances natively. All integration coverage depending on `vroom`, `ortools`, and `osrm` engines fails natively (`SOLVER_UNAVAILABLE` exceptions due to missing solver binaries). Tests explicitly opted in with `TIRODHAN_ROUTING_ENGINE_TESTS=1` result in subprocess execution failures.

Because of this blocker, no claims of correctness are made about the end-to-end native routing scenarios or PostgreSQL integration scenarios, as they cannot run in this sandbox.

## 2. Execution Run Summary

We executed tests across available routing, integration, and unit boundaries.

### Routing Component Tests (`tests/routing/`)
- **Without Engine Flags (`TIRODHAN_ROUTING_ENGINE_TESTS=0`):** 85 Passed, 28 Skipped
- **With Engine Flags (`TIRODHAN_ROUTING_ENGINE_TESTS=1`):** 97 Passed, 13 Failed, 3 Skipped (all failures correspond to missing solver processes/binaries e.g. vroom)

### Integration Tests (Database Dependent)
- `test_compaction_execution.py`: 12 Skipped (Database missing)
- `test_fleet_hypothesis_stateful.py`: 8 Skipped (Database missing)
- `test_fleet_hypothesis_native.py`: 6 Skipped (Native solver missing)

## 3. Findings

### Finding 1: Native Solver Missing Blockage
- **Type:** Environment Failure
- **Severity:** P0 (for QA environment only)
- **Description:** The automated agent sandbox is unable to mount Docker containers for `vroom`, `osrm`, or PostgreSQL.
- **Remediation:** Provide standard host binaries (e.g. `sudo apt install vroom ortools postgresql`) or enable containerd namespaces.

### Finding 2: Missing Hypothesis Dependency in Routing Tests
- **Type:** Coverage Gap / Setup Defect
- **Severity:** P2
- **Description:** Direct invocation of `pytest tests/routing` fails due to `conftest.py` relying on `hypothesis`, which is defined under `[dev]` optional dependencies.

## 4. Conclusion
Due to environment restrictions, full adversarial validation against realistic databases/native binaries was not possible. The Python logic execution itself remains green for available unit bounds. Future QA passes require a test harness that allows full native processes or Docker support to certify the VROOM/OR-Tools integration accurately.
