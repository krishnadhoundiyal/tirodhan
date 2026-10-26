# Tirodhan V2 Routing QA Report (Increment 9)

**Target SHA:** 3da3378402032fbca1d08e0c3335ff0b70b8c2f6
**Triggering SHA:** 3da3378402032fbca1d08e0c3335ff0b70b8c2f6
**Date:** 2024-10-26T15:02:00Z
**Environment Constraints:** Docker daemon has mount failures `failed to mount /tmp/containerd-mount... invalid argument`. Execution environment cannot run OSRM, VROOM, PostgreSQL, or PostGIS via docker containers.

## 1. Execution environment block
The automated sandbox execution environment blocked container startup for PostgreSQL and OSRM with `docker: Error response from daemon: failed to mount... invalid argument`.
Following the AGENTS.md / QA Mandate directive: "If OSRM, Docker, native solvers or PostgreSQL cannot be launched in the Jules environment, report the precise blocked coverage rather than presenting simulated or skipped tests as passed", no simulated tests were run or claimed as passing for native routing, simulation, or hypothesis suites requiring database access.

Only baseline unit tests without docker-based dependencies are recorded here, along with a static finding.

## 2. Default Test Execution
Executed:
```bash
pytest -q tests -p no:cacheprovider
```
**Result:** 751 passed, 1060 skipped (all missing TIRODHAN_TEST_DATABASE_URL or explicitly skipped native/benchmark requirements), 5 warnings in 50.05s.
No regressions were found within the limited non-native suite.

## 3. Findings

### Coverage gap: Native Routing Tests / E2E
**Category:** Environment failure / Coverage gap
**Severity:** P1 (Blocking for CI QA)
**Details:** Because the sandbox Docker environment failed to mount container layers correctly, the native testing scripts (e.g. `tests/routing/run-regional.ps1`) and native hypothesis tests were entirely blocked. No operational simulation, regional admission benchmarks, or new adversarial inputs could be natively tested. This represents a missing validation step that must be performed natively on a functional Docker host before production deployment.

### Recommendation
Fix the Docker `overlayfs` mount issue on the CI/QA runners, or provision a standalone PostGIS/OSRM testing service to run against in this environment.
