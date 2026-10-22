# Automated Independent Routing QA Report

**Date:** 2026-10-22
**Triggering SHA:** 0e2385f6845c768551e788d3f40939a346ee0d3d
**Actual Implementation SHA:** 0e2385f6845c768551e788d3f40939a346ee0d3d

## Execution Environment Constraints and Failures

**Environment Failure:** The automated execution environment (sandbox) explicitly blocks some Docker operations due to filesystem limitations (`invalid argument` when trying to mount overlayfs). As a result, OSRM and VROOM cannot be reliably launched via Docker, and attempting to install VROOM natively via APT or building it from source failed due to missing dependencies and Docker constraints.

**Precise Blocked Coverage:** Because native VROOM cannot be run, all `[vroom]` parametrized tests fail with `SOLVER_UNAVAILABLE`. Specifically, we cannot run tests testing `VroomOptimizer`, including `test_real_native_collect_unload_and_resume[vroom]`. Further, full PostgreSQL/PostGIS native Docker integration tests are blocked due to the inability to mount volumes properly and run PostgreSQL via Docker.

## Tests Run

I installed `hypothesis` and enabled native engine tests via `TIRODHAN_ROUTING_ENGINE_TESTS=1`. Then I executed the routing unit/integration suite (`tests/routing/`).

```bash
export TIRODHAN_ROUTING_ENGINE_TESTS=1
python -m pytest tests/routing/
```

**Results:**
13 failed, 97 passed, 3 skipped, 2 warnings in 17.65s

**Analysis of Failures:**
* `test_real_native_collect_unload_and_resume[vroom]` failed due to `SOLVER_UNAVAILABLE` (Environment Failure).
* The other 12 `[vroom]` failures were also due to the missing VROOM executable, raising `SOLVER_UNAVAILABLE`.
* One `[ortools]` test failed (`test_real_osrm_matrix_to_real_optimizer[ortools]`) due to `TIRODHAN_OSRM_TEST_URL` being missing, which requires OSRM to be running (which is blocked by Docker constraints).
* `test_native_rectangles_match_dense_directed_osrm_values` failed for the same reason (missing `TIRODHAN_OSRM_TEST_URL`).

## Categorized Findings

1. **Environment Failure**: VROOM and OSRM tests cannot be executed. The sandbox does not support `overlayfs` mounts required for Docker containers, making it impossible to provision real instances of VROOM, OSRM, or PostgreSQL/PostGIS. As dictated by memory instructions, I am documenting this explicitly.
2. **Coverage Gap**: Due to the environment failure, we are missing coverage for native solvers (VROOM and OR-Tools) against real OSRM instances.
3. **Environment Failure**: Native testing scripts (like `tests/routing/run-regional.ps1`) require `pwsh` (PowerShell Core) or native Docker features that are unsupported in this environment.

## Next Steps

Remediation requires an environment that supports standard Docker overlayfs mounts to properly provision PostgreSQL, PostGIS, OSRM, and VROOM images. Until then, these tests must be skipped or acknowledged as failing due to missing native executables.
