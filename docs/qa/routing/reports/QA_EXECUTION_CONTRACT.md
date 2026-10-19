# QA Execution Contract

## Booking Lifecycle
- Bookings enter the system and trigger events like `CustomerFinancialStatusChanged`.
- The `routing-financial-wakeup` inbox message processor evaluates financial status changes. If payment is confirmed (`PAYMENT_CONFIRMED`), it triggers a `wake` call for the `BOOKING_FINANCIAL_FACT` reason.

## Regional Ownership & Triggering
- `regional_wake(session, region, now, urgent=urgent)` is called to schedule a fleet wakeup.
- A `FleetWakeup` record is updated/inserted for the scope (region).
- `prepare_due` evaluates if a region is due for planning based on time or new financial facts.
- The entry point for processing a region is `run_region` in `src/tirodhan/modules/routing_operations/regional_planner.py`.
- `run_region` evaluates `FleetPolicy` configurations. There isn't a direct DB column called "accumulation threshold", but we can trigger it based on the number of bookings arriving or interval-based triggering (`minimum_interval_seconds`).

## Planner Invocation
- `run_region` captures a snapshot (`capture_snapshot`), checks readiness (`regional_ready`), and extracts problems.
- Real problems are passed to `run_problem` or `refresh_selection` (or inside `run_region` directly through candidate generation).
- `run_region` builds private routing problems for incumbent routes, then uses `admit` to determine which new bookings to evaluate based on capacity and horizon.
- It then evaluates `flexible.py` supplementary offers or falls back to standard capacity constraints.

## Solvers & Verification
- VROOM: `OptimizerEngine.VROOM`.
- OR-Tools Routing: `OptimizerEngine.OR_TOOLS`.
- OR-Tools CP-SAT Coordinator: used during selection / consolidation phase (`SelectionEngine`).
- Measurements are recorded in `FleetCycle` and `RoutingShadowRun`.

## Overlap & Constraints
- The `FleetPolicy` and `RegionalPolicy` govern horizon (`horizon_seconds`).
- Vehicle/Rider overlap is possible if assigned across regions, but constraints strictly ensure physical feasibility.
- Previously completed or protected assignments must be preserved. `ready_snapshot` checks existing incumbent routes, and they act as seeds for the solvers, ensuring they are not violated.
