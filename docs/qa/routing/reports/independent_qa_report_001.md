# Tirodhan Routing: Independent QA Report

## Overview
- **Execution Date**: 2026-10-19
- **Triggering SHA**: `eefabf5c2174533653f71bdf7e961a039c46b261`
- **Actual Implementation SHA**: `eefabf5c2174533653f71bdf7e961a039c46b261`
- **SHA Mismatch**: No
- **Execution Environment**: Jules Sandbox with Python 3.12, pytest 8.4.2, hypothesis 6.168.5, and OR-Tools 9.14.6206.

## Level 1: Existing Regression
Ran existing routing tests (`pytest tests/routing`).
- `test_regional_selection.py::test_real_native_collect_unload_and_resume[vroom]` failed due to `SOLVER_UNAVAILABLE` because VROOM was not available in this environment.
- Other unit tests mostly passed or skipped appropriately due to missing native infrastructure (OSRM/VROOM pins). OR-Tools was successfully tested using local dependency.
- **Coverage Gap / Environment Failure**: The VROOM solver is required for `vroom` integration tests but unavailable in the sandbox. Tests skipped.

## Level 2: Adversarial/Model-Based
Created an adversarial test case to challenge OR-Tools optimization limits:
- **Scenario Description**: A single vehicle must visit two overlapping time-window stops with constrained capacity `(1,)`. The vehicle cannot serve both simultaneously without returning to the depot, or it should at least serve one and drop the other. This tests the solver's edge-case handling of partial feasibility and drop logic.
- **Expected Outcome**: The vehicle should pick up one cargo lot, proceed to a receiving point to discharge, and then potentially serve the second lot if time permits, or leave one unserved if constraints strictly prevent it.
- **Actual Outcome**: The solver succeeds in returning a proposal, but it leaves **both** stops unserved (`unserved_stop_ids`), rather than optimally serving at least one stop.
- **Finding**: **Optimization Weakness**. The `ORToolsOptimizer` currently fails to select a feasible partial subset when capacity constraints prevent serving the entire batch in one go, opting instead to serve nothing.
- **Severity**: P1 - In constrained capacity environments, it's crucial that partial routes are returned instead of entirely rejecting all work in the proposal.
- **Remediation**: Revisit the drop penalties and capacity/time constraint formulations in `ortools_worker.py`. Ensure that the solver explores intermediate discharges or correctly accepts partial routing instead of dropping all optional nodes when capacity is tight.

## Deliverables Status
- `tests/routing/test_independent_qa_mandate.py` created to reproduce and assert the finding (using `pytest.xfail`).
- This report captures findings without modifying the production code.
- A QA pull request will encapsulate these artifacts.
