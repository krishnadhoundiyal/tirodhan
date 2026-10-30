# Routing QA Report

## Execution Context

* **Triggering SHA**: `3da3378402032fbca1d08e0c3335ff0b70b8c2f6`
* **Actual Implementation SHA**: `3da3378402032fbca1d08e0c3335ff0b70b8c2f6`
* **SHA Mismatch**: None
* **Execution Environment**: Jules automated sandbox
* **Environment Limitations**: Docker operations are blocked in this environment (cannot mount overlayfs). PostgreSQL/PostGIS, OSRM, and native solvers (vroom) cannot be fully launched/run. Tests depending on native dependencies (like `vroom` binary or `osrm` urls) will fail with `SOLVER_UNAVAILABLE` or `KeyError`.

## Test Execution Results

Command executed: `TIRODHAN_ROUTING_ENGINE_TESTS=1 pytest tests -q`

**Counts**:
- 766 passed
- 16 failed
- 1029 skipped
- 7 warnings

**Failures (Environment Blocked)**:
All 16 native test failures are environment-related (vroom binary not available, missing OSRM matrix API):
- `FAILED tests/integration/test_fleet_hypothesis_native.py::test_native_proposal_and_refinement_against_exhaustive_oracle[VROOM]`
- `FAILED tests/integration/test_fleet_hypothesis_native.py::test_real_engines_minimized_extra_vehicle_regression[VROOM]`
- `FAILED tests/integration/test_fleet_hypothesis_native.py::test_real_engines_on_documented_local_search_limit[VROOM]`
- `FAILED tests/routing/test_admission_native.py::test_many_identical_load_lots_with_zero_time_still_preserve_identity[VroomOptimizer]`
- `FAILED tests/routing/test_native_engines.py::test_real_engine_initial_cargo_partial_discharge_and_protected_prefix[vroom]`
- `FAILED tests/routing/test_native_engines.py::test_prefix_can_include_pickup_without_forgetting_its_cargo[vroom]`
- `FAILED tests/routing/test_native_engines.py::test_required_impossible_work_does_not_produce_valid_proposal[vroom]`
- `FAILED tests/routing/test_native_engines.py::test_optional_unreachable_work_is_explicitly_unserved[vroom]`
- `FAILED tests/routing/test_native_engines.py::test_both_real_engines_evaluate_same_snapshot_concurrently`
- `FAILED tests/routing/test_native_engines.py::test_multiple_profiles_and_hard_ownership[vroom]`
- `FAILED tests/routing/test_native_engines.py::test_facility_opening_gaps_are_hard_constraints[vroom]`
- `FAILED tests/routing/test_native_engines.py::test_real_osrm_matrix_to_real_optimizer[vroom]`
- `FAILED tests/routing/test_native_engines.py::test_real_osrm_matrix_to_real_optimizer[ortools]`
- `FAILED tests/routing/test_orchestration_engines.py::test_break_with_cargo_preserves_window_and_physical_balance[VROOM]`
- `FAILED tests/routing/test_rectangular_roads.py::test_native_rectangles_match_dense_directed_osrm_values`
- `FAILED tests/routing/test_regional_selection.py::test_real_native_collect_unload_and_resume[vroom]`

## Findings Categorization

1. **Environment failure**: Docker cannot be used for provisioning OSRM/PostgreSQL/VROOM in this environment, explicitly blocking full operational native regression and E2E simulation.
2. **Coverage gap**: We were unable to execute the full E2E simulation (PostgreSQL/PostGIS) and adversarial/native solver scenarios (`vroom`) due to infrastructure limitations. We must acknowledge this explicit block rather than claiming tests passed.

3. **Coverage Addition (Adversarial/Model-based testing)**: Added `tests/integration/test_qa_adversarial.py` to test Python-native validation logic `avoid_breaks` via Hypothesis property-based testing. This explicitly verifies that the scheduling logic around vehicle breaks correctly adjusts task starts to avoid overlaps without modifying production invariants, and tests for cascading shift operations across multiple breaks, achieving successful deterministic validation independent of native solver availability.
