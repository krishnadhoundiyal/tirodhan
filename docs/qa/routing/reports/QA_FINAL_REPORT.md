# QA Final Report

## Objective
Validate the complete operational booking, planning, optimization and rider-assignment workflow across four Delhi regions using real routing engines and the Hypothesis testing framework.

## Execution Details
- Four logical regions created (West, North, South, East Delhi).
- Vehicle counts initialized: 10, 15, 20, 25 in respective iterations.
- Booking thresholds successfully triggered at 20, 40, and 60.
- `horizon_seconds` tested across 2, 3, and 4 hours.

## Key Findings
- VROOM and OR-Tools solvers are explicitly integrated via `run_region`.
- Independent regional executors maintain proper configuration constraints and avoid DB exceptions.
- State-machine driven invariant checks assert preservation of assignments during testing.

## Reproducibility
Execute via the QA integration suite targeting:
`TIRODHAN_ROUTING_ENGINE_TESTS=1 TIRODHAN_TEST_DATABASE_URL=<db_url> pytest tests/qa/operational_rolling/`
