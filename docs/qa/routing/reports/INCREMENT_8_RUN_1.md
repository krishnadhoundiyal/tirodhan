# QA Report: V2 Regional Routing Increment 8

## Context
**Triggering SHA:** 00140fc71311b6b014e8cc5e81999eb20c7ffe88
**Actual Implementation SHA:** 00140fc71311b6b014e8cc5e81999eb20c7ffe88

## Environment Failures and Blocked Coverage
The autonomous execution environment prevents full native routing evaluation due to Docker integration restrictions:
- **Docker in Docker restrictions:** The Jules sandbox cannot run `docker` or `docker compose` to bring up the full multi-container test harness (OSRM, PostGIS) required for native testing and simulation via `tests/routing/run-regional.ps1`.
- Docker volume mounts and container creation failed with `failed to mount /tmp/containerd-mount...` and exit code 125, explicitly blocking the native integration pathways.
- Thus, OSRM road matrix acquisition and native CP-SAT/VROOM combinations with real database races were blocked.

**Finding 1 (Environment Failure):** Blocked Native Docker Harness
- **Category:** Environment failure
- **Severity:** P0 for QA execution
- **Description:** Docker sandbox limits blocked the PostGIS/OSRM orchestration required by `run-regional.ps1`.
- **Remediation:** QA must be run in a VM or environment with full privileged Docker capability. For the scope of this isolated run, we report the block rather than falsely claiming tests were fully executed.

## Unit Test Execution (Fallback)
We executed the core unit test suite via `python -m pytest tests`.
- **Counts:** 745 passed, 1060 skipped, 5 warnings in 42.41s
- **Note:** The 1060 skipped tests represent the PostGIS `TIRODHAN_TEST_DATABASE_URL` tests and native engine `TIRODHAN_ROUTING_ENGINE_TESTS=1` tests that could not execute due to the blocked Docker setup.

## Adversarial/Hypothesis Scenarios (Blocked)
Without the underlying test fixtures (PostGIS and OSRM), native Hypothesis/CP-SAT adversarial scenarios could not execute against the real engine boundaries. Hypothesis itself was missing from the `.[routing]` constraints initially but resolved via `pip install hypothesis`.

## Deliverables
- A QA PR branch will be created with this report. No production code was modified.
