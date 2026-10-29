# QA Routing Report

## Environment Execution
- Genuinely new adversarial scenarios added via `tests/routing/qa_reports/test_adversarial_qa.py`.
- Tested Hypothesis state machines for routing.

## Environmental Limitations
- The automated execution environment (sandbox) blocks Docker operations, preventing local PostgreSQL/PostGIS and OSRM provisioning.
- VROOM and OR-Tools solvers are unable to be accessed properly since binaries/execution environment block it or they are not provisioned.
- These dependencies are explicit environment failures instead of claiming tests passed.

## Trigger Details
- Triggering SHA: 3da3378402032fbca1d08e0c3335ff0b70b8c2f6
- Actual Implementation SHA: 3da3378402032fbca1d08e0c3335ff0b70b8c2f6
- SHA Mismatch: False (main has not advanced since the trigger).

## Findings
- **Coverage Gap**: OSRM, Docker, native solvers, and PostgreSQL could not be launched in the Jules environment. Native solver opt-ins were correctly skipped rather than falsely claiming execution without resources.
- **Environment Failure**: Due to sandbox constraints, PostGIS, OR-Tools, VROOM, and OSRM cannot be executed completely end-to-end for integration tests that require them, leading to 28 skipped native opt-in tests.

## Metrics
- 85 baseline tests passed, 28 skipped (all related to explicit opt-in missing dependencies for native solvers/OSRM local graphs).
- 1 new Hypothesis adversarial stateful test added and passed.
