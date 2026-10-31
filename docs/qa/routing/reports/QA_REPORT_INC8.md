# Autonomous Routing QA Report

## Metadata
- Triggering SHA: bac389241161fdd70a389d96e12585c47b101a19
- Actual SHA: bac389241161fdd70a389d96e12585c47b101a19
- Date: 2026-10-31 09:21:28
- Environment: Local execution (pytest natively via python -m pytest tests/routing/...)

## Execution Summary

`pytest tests/routing/test_admission_native.py -v` and `pytest tests/routing/test_adversarial_qa.py -v` were executed.
Due to missing dependencies (e.g. `vroom` binary is unavailable natively in this automated environment without Docker configuration limitations, and Docker native tests encountered permission failures during image building `/var/lib/apt/lists/.wh.auxfiles`), a full automated regression using OSRM, OR-Tools, and VROOM via PowerShell/Docker could not be executed natively.

A set of adversarial tests utilizing Hypothesis to stress-test TimeWindow parameter limits was executed successfully as an operational capability check.

## Environment & Coverage Blockages

- `vroom` and `ortools` solvers are missing or lack dependencies for native process launching (`create_subprocess_exec` for `'vroom'`).
- The docker environment setup failed during `tests/routing/Dockerfile` build on step `rm -rf /var/lib/apt/lists/*` with `failed to convert whiteout file "var/lib/apt/lists/.wh.auxfiles": operation not permitted`.
- Consequently, native engine tests were skipped or explicitly failed (e.g. `test_many_identical_load_lots_with_zero_time_still_preserve_identity` with `VroomOptimizer`).

**Precise blocked coverage:**
- Native OR-Tools/VROOM integration runs for assignment and selection (`tests/routing/test_native_engines.py`, `test_admission_native.py`).
- PostgreSQL real execution metrics (Powershell `run-regional.ps1`).

## Findings

1. **Environment Failure:** `vroom` binary is not available. Native execution scripts require Docker, which fails to build the `routing-tests` base image due to permission errors on `/var/lib/apt/lists/`.
   - **Severity:** P0 (Environment Block)
   - **Business Rules:** Blocks validation of core routing selection algorithms and matrix handling.
   - **Remediation:** Fix the Dockerfile (e.g., modifying `rm -rf /var/lib/apt/lists/*` to accommodate `overlayfs` build environment) or natively install `vroom`.

2. **Coverage Gap:** Existing Python CI environment defaults to missing `hypothesis` test generation dependency.
   - **Severity:** P2
   - **Remediation:** Ensure `hypothesis` is in `dev` dependencies.
