# QA Report

Triggering SHA: 3da3378402032fbca1d08e0c3335ff0b70b8c2f6
Actual Implementation SHA: 3da3378402032fbca1d08e0c3335ff0b70b8c2f6

## Baseline Execution

Existing tests were run with the following command:
`python -m pytest tests/routing/`

Outcome: 87 passed, 28 skipped in ~5.84s.

## Findings & Categorizations

- **environment failure**: Opt-in real engines skipped because native dependencies (OSRM, PostGIS, real native engine solvers such as VROOM and OR-Tools) are intentionally blocked or not available in the CI sandbox environment.
- **coverage gap**: Due to the environment failure above, realistic adversarial and end-to-end operational simulations against the real native engines cannot be performed in this execution environment.

## Additions
- Added new Hypothesis property-based bounding tests in `test_adversarial_qa.py`.
- Added mock operational simulation synthetic payload in `fixtures/operational_sim.json`.
