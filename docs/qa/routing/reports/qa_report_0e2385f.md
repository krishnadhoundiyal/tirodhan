# QA Routing Execution Report

**Date:** 2026-10-22
**Triggering SHA:** `0e2385f6845c768551e788d3f40939a346ee0d3d`
**Actual Implementation SHA:** `0e2385f6845c768551e788d3f40939a346ee0d3d` (No mismatch)
**Execution Environment:** Automated independent QA agent environment (Linux sandbox). Note: Docker daemon privileges and local OSRM/PostgreSQL database instances were unavailable, blocking native end-to-end integration and native solver execution.

## 1. Existing Regression Tests

### Summary
The baseline existing regression suite for `tests/routing` was executed using mocked solvers where appropriate.
Due to environment restrictions (no PostgreSQL/PostGIS, no OSRM server, no native solvers (vroom/ortools) running via local Docker), real-engine dependent tests were explicitly configured to skip or naturally failed/skipped due to dependency absence.

### Command Log
```bash
TIRODHAN_ROUTING_ENGINE_TESTS=0 python -m pytest tests/routing
```

### Results
- **Total Tests Run:** 113
- **Passed:** 85
- **Skipped:** 28 (Explicitly skipped due to missing pinned native solver environments, local OSRM graph, etc.)
- **Execution Time:** ~4.11s

*Note: Artificially forcing `TIRODHAN_ROUTING_ENGINE_TESTS=1` resulted in 13 failures and 3 skipped tests as expected, due to missing environment services.*

## 2. Adversarial/Model-Based Tests

### Implementation
A genuinely new adversarial property-based test scenario was introduced in `tests/routing/test_adversarial_qa.py` using `hypothesis`. The test generates completely hostile `RoutingProblem` inputs featuring:
- Randomized time windows (some inverted, some spanning large durations)
- Random vehicle limits and capacities (potential misalignments with required lots)
- Edge case limits on required quantities versus vehicle constraints.

The validation module (`tirodhan.modules.planning.routing.validation.validate_proposal`) was tested for resilience to ensure it returns explicit `RoutingValidationError` exceptions on malformed proposals against hostile problem contexts without crashing (e.g. escaping `KeyError`, `IndexError`, or `ValueError`).

### Command Log
```bash
python -m pytest tests/routing/test_adversarial_qa.py -v
```

### Results
- **Passed:** 1 (Executes up to 200 adversarial property-based scenarios generated via hypothesis).
- **Outcome:** The validation boundaries correctly caught exceptions or safely bypassed logic via controlled exceptions rather than uncontrolled crashes.

## 3. Findings & Categorization

1. **Environment Failure (P0 for CI/CD Pipeline execution)**
   - **Details:** The current automated QA sandbox restricts the usage of local Docker loops required to spin up PostGIS, OSRM router instances, and native solvers (vroom/ortools).
   - **Affected rules:** End-to-end operational simulation and native matrix generation tests cannot run.
   - **Recommendation:** Document precisely what test coverage is dropped in CI logs (as done here). For local/isolated reproduction, developers must supply a trusted Docker-capable environment as documented in `tests/routing/README.md`.

2. **Coverage Gap (P2)**
   - **Details:** With native components unavailable, we lose direct verification of `OSRMMatrixProvider._fetch` handling malformed matrix distance coordinates dynamically.
   - **Recommendation:** Consider adding more robust unit tests that mock the HTTP response for `OSRMMatrixProvider._fetch` to synthesize corrupted matrix tables locally and verify `INVALID_ROAD_MATRIX` exceptions.

3. **Optimization Weakness (P2)**
   - **Details:** While generating the adversarial cases, there is an edge case where missing bounds in constraints could cause solvers to spend excessive time searching. The current Python-side validation gracefully handles invalid proposals but relies on the solvers to not hang on malformed hostile inputs.
