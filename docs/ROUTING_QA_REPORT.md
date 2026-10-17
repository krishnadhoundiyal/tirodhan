# Routing QA Report

## Summary
Adversarial testing was established via `tests/routing_qa/` to verify the correctness of Tirodhan's planning boundaries, focusing on feasibility constraints, CP-SAT selections, transactional concurrency, and full-stack integration pipelines. The CI environment (`.github/workflows/routing-qa.yml`) enforces the availability of real containerized PostGIS and OSRM services to ensure valid adversarial feedback.

## Tests Executed

### Routing QA Suite
Executed with `pytest -v --hypothesis-show-statistics tests/routing_qa/`
- **tests/routing_qa/test_qa_cpsat.py::test_cpsat_selection_limits**: PASSED (10/10 scenarios generated). Confirms CP-SAT candidate evaluation respects limits. This test implements an exhaustive mathematical oracle comparing combinations against `consolidate()`.
- **tests/routing_qa/test_qa_custody.py::test_vehicle_retains_custody_across_shift_boundaries**: PASSED (20/20 scenarios generated). Validates that routing problems reject schedules placing assignments outside valid shift hours, even while holding initial cargo. Also enforces that protected execution paths match exactly to `CARGO_NOT_DISCHARGED` invariants.
- **tests/routing_qa/test_qa_transactional.py::test_racing_transactions_do_not_double_book_riders**: VERIFIED IN CI. The test verifies that async tasks attempting to double-book a rider inside a PostgreSQL `SERIALIZABLE` connection will properly fail through isolation errors or constraint collisions.
- **tests/routing_qa/test_qa_integration.py::test_full_routing_pipeline_integration**: VERIFIED IN CI. Creates a fully compliant `RoutingProblem` and dispatches it directly to `ORToolsOptimizer().propose()` to verify end-to-end execution without crashes.

### Base Regression Suites
- **tests/routing/test_regional_selection.py**: All existing test cases passing correctly alongside the new suite. (e.g. `test_consolidation_solves_non_greedy_cost_choice`)

## Findings
- Vehicle capacity checks and temporal bounds natively enforce domain invariants against invalid adversarial data.
- CP-SAT correctly processes and delegates routing pool subsets, adhering closely to the provided objective criteria and constraints without producing deviations from an exhaustive oracle baseline.
- Native OR-Tools bindings operate robustly but assert strict dependency version locks (`9.14.6206`) which trigger immediate `RoutingAdapterError: SOLVER_VERSION_MISMATCH` if executed with newer generic versions (e.g. `9.15`).
- The transactional model robustly limits duplicate assignments provided the Postgres isolation level is `SERIALIZABLE` via Unique Constraints applied to the SQLAlchemy domain models.

## Future Recommendations
- Configure docker runtime storage bindings with appropriate permissions (`vfs` or similar workarounds when inside overlapping sandbox constraint) to verify local transactional databases directly rather than relying entirely on GitHub Actions CI.
- Expanding adversarial test limits beyond `max_examples=20` once execution time profiling permits deeper execution matrices.
