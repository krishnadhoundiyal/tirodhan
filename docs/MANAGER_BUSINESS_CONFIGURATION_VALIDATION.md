# Manager administration and replacement validation

Base: `7892d64877d918f218e33b29c2b1fad491fbba49`.
Branch: `codex/manager-business-configuration`.
Implementation contracts: [manager configuration report](MANAGER_BUSINESS_CONFIGURATION.md).
No production routing activation, deployment, frontend changes or new infrastructure.

## Executed checks

The Python 3.12 CI-compatible runs below use
`python:3.12-slim`, the repository's `.[dev]` dependencies and a task-owned disposable
`postgis/postgis:17-3.5` database. PostGIS, topology, fuzzystrmatch and TIGER extensions
are installed to exercise extension-owned schema reflection. No production data is used.
Measured server versions are PostgreSQL **17.5** and PostGIS **3.5.2**.

| Run | Actual result |
|---|---|
| Final focused database, migration, replacement, compatibility and contract checks | 48 passed, 1 warning, 77.89 seconds |
| Delivery-source real-engine replacement acceptance | 2 passed, 2 warnings, 60.70 seconds |
| Existing fleet/API/payment boundaries | 61 passed, 72.90 seconds |
| Repaired legacy migration-fixture compatibility | 37 passed, 70.79 seconds |
| Additional legacy admission and foundation migration checks | 18 passed, 83.18 seconds |
| Financial legacy migration and live revocation race checks | 7 passed, 55.47 seconds |
| Final history-preserving downgrade guard regressions | 2 passed, 24.54 seconds |
| Superseded Python 3.12 mounted-source run, deliberately interrupted after fixes | 433 passed, 12 failed, 17 skipped, 1 warning, 992.04 seconds; partial run |
| Completed Python 3.12 full fixed-source run | 1,470 passed, 1 failed, 67 skipped, 10 warnings, 965.37 seconds |
| Replacement gate and missing-coordinator unit regressions | 13 passed, 5.12 seconds |
| Corrected Python 3.12 check without optional coordinator | 13 passed, 1 explicitly skipped, 4.10 seconds |
| Earlier Python 3.13 default run without integration opt-ins | 621 passed, 1 failed, 940 skipped, 5 warnings, 66.44 seconds |

These runs overlap; their counts must not be summed as unique coverage. Native tests
are explicitly enabled in their dedicated runner. Unavailable database/native/optional
routing tests in a default environment are skips, not evidence of success.

The Python 3.13 failure is the unchanged
`tests/unit/test_twofactor.py::test_timeout_traceback_and_http_logging_hide_sensitive_url`.
Python 3.13 includes a test source literal in the rendered traceback. Neither that
test nor the TwoFactor integration was modified in this increment. The Python 3.12
result is reported separately rather than treating this failure as a pass.

The earlier broad focused run had 176 passes, 9 failures and 8 skips. All nine failures
were introduced compatibility issues in old migration fixtures: they created newly
audited state and then requested a deliberately lossy downgrade, or deleted a referenced
fleet configuration. Fixtures now seed financial history at their historical migration
boundary; current adopted-history downgrade guards remain intact. The repaired checks
passed. No business history guard was weakened to make tests pass. The full CI-compatible run
also exposed older admission/foundation/financial migration fixtures and one test that
deleted a role to simulate revocation. Those fixtures now seed before administration
adoption; the race uses the existing revocation fields. A narrowly scoped legacy-writer
fixture suppresses only new capture/defaults during old regional fixture construction,
asserts no revisions exist first, and restores capture afterwards. It never deletes or
rewrites revisions or disables immutable-history guards. Normal manager tests and the
new migration preservation tests retain full capture. Their focused rerun results are
listed above.
The superseded run had the old test definitions loaded. Its 12 failures are the
admission/history, foundation migration, financial migration and role-deletion fixture
cases described above (including three foundation failures cascading from the earlier
failed downgrade). The final full run uses a fixed tracked-source snapshot on the
container's Linux filesystem, with the corrected fixtures, rather than combining
partial counts from different source versions.
Final review subsequently hardened the two new rollback guards against a caller's
baseline-like reason and newly adopted decision/activation metadata on legacy requests.
Those migration changes and the added regression were tested separately in the final
two-test run above. The completed full run also exposed one optional-dependency failure:
the collected replacement test requires CP-SAT coordination, while CI's base `.[dev]`
environment has no OR-Tools distribution. That dependency is now explicit in the test;
it is exercised in the supported solver-enabled environment, not claimed as a default
environment pass. Coordinator import/metadata failure now becomes bounded UNKNOWN
rather than an unhandled exception. Both missing-module and missing-distribution cases
have unit regressions. The fixed-source full run predates those final changes;
the corrected Python 3.12 delta check and GitHub checks on the delivered commit cover
the final source. The final native run above also uses the delivery source.

## Reproduction

Use a disposable PostgreSQL/PostGIS database exclusively: integration fixtures reset
the application schema. Never point these commands at an operational database.

```powershell
$env:TIRODHAN_TEST_DATABASE_URL = '<disposable PostgreSQL asyncpg URL>'
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
mypy
pytest -q
```

Focused run in the pinned routing image, with `src` and `tests/integration` on
`PYTHONPATH`:

```text
pytest -q tests/integration/test_manager_business.py
  tests/integration/test_manager_migration.py
  tests/integration/test_manager_replacement_safety.py
  tests/integration/test_regional_capacity_recovery.py
  tests/unit/test_replacement_gate.py tests/unit/test_replacement_replay.py
```

Real-engine run (local cached road graph and pinned image required):

```powershell
& tests/routing/run-geographical.ps1 -SkipBuild `
  -Selection tests/integration/test_manager_replacement_native.py `
  -ArtifactsDirectory .tools/manager-business/native-validation
```

Measured native versions: OSRM **6.0.0**, VROOM **1.15.0**, OR-Tools **9.14.6206**;
native image Python **3.13.5**. The two acceptance tests create paid bookings through
hold, checkout and provider acceptance. Their initial split route is an explicit
incumbent fixture submitted through real validation/activation. Replacement evaluation
uses both engines, directed road matrices and the independent regional/facility gate.
Subsequent regional activation and pickup/handover execution use production services.
The independent safety tests also use synthetic proposals to isolate rejection paths;
those are not represented as native-engine evidence.
Gateway observations are synthetic provider fixtures; no live customer charge is made.

## Acceptance coverage

| Invariant/scenario | Automated evidence |
|---|---|
| Manager create/edit, attributable before/after, exact replay, conflicting replay | `test_manager_history_replay_conflict_and_immutable_before_after` |
| Concurrent manager edits have one valid winner; live role revocation | `test_manager_concurrency_and_live_revocation` |
| One flat fee across multiple items/quantities; immutable price revision | `test_flat_pickup_price_is_versioned_and_independent_of_item_count` |
| Booking, Payment and item amounts agree; replay never requotes | `test_configured_price_snapshot_and_completed_booking_replay` |
| Old/new image identity and metadata remain resolvable, immutable asset key | `test_product_revision_keeps_immutable_previous_asset` |
| HTTP authorization, historical reads, upload/finalization MIME checks | `test_manager_http_history_images_and_live_authorization` |
| Create-only Azure SAS, scoped product prefix | `tests/unit/evidence/test_azure_media.py` |
| Existing identity rider provisioning/revocation and profile revisions | `test_rider_provisioning_role_revocation_and_profile_history` |
| Receiving PostGIS coordinates, windows and immutable fact comparison | `test_receiving_postgis_guards_and_reused_duty_history` |
| Fleet JOIN/LEAVE and shift confirmation/close attribution | `test_reused_fleet_membership_and_duty_changes_have_attributable_history` |
| Receiving/vehicle/region structural edits cannot discard accepted promises | `test_live_commitments_guard_receiving_vehicle_and_region_edits` |
| Populated financial migration preservation, strict schema comparison, downgrade guard | `test_manager_upgrade_preserves_populated_financial_facts_and_schema` |
| Caller-supplied baseline reason cannot bypass provenance checks; new decision metadata survives attempted downgrade | Both tests in `test_manager_migration.py` |
| Rider request retained; manager incident does not fabricate rider consent | Native tests and existing `test_regional_capacity_recovery.py` |
| Multiple ranked validated candidates, stable published order and provenance | `test_native_options_preserve_busy_candidate_work_and_activate` |
| Selected rider's existing paid work survives replacement | Both native acceptance tests |
| New booking after evaluation makes previous recommendation stale | First native acceptance test |
| Concurrent manager selections produce one winner; exact selection replay | First native test and `test_pending_approval_can_select_fresh_evaluation_without_losing_old_constraint` |
| Selected target loses role; activation stays pending; authorized recovery resumes | First native acceptance test |
| Approved pending choice can refresh without releasing the old constraint early | `test_pending_approval_can_select_fresh_evaluation_without_losing_old_constraint` |
| STARTED household cannot be interrupted or released | `test_started_household_cannot_be_interrupted_or_released` |
| Matrix timeout, engine timeout and invalid proposal yield UNKNOWN; retry recovers | `test_incomplete_search_is_unknown_preserves_ownership_and_recovers` |
| Unavailable optional CP-SAT coordinator cannot certify a candidate | `test_missing_coordinator_never_certifies_a_replacement` |
| Revoked role, closed shift, closed facility and unaccepted flexible capacity excluded | `test_live_conflicts_never_publish_unsafe_candidates` |
| Capacity, appointment, shift, facility and protected-prefix constraints survive both engine contracts | `tests/unit/test_replacement_gate.py` |
| Collected cargo bound to actual custodian, historical pickup attribution unchanged | Custodian unit regression and second native acceptance test |
| Whole failed cart retains individual lot/booking custody; approval is not transfer | `test_native_failed_cart_keeps_individual_custody_until_transfer_and_activation` |
| Donor/receiver evidence, sequential lot transfers, replay and route activation | Second native acceptance test |
| Actual receiving handovers clear new custodian's cargo without rewriting original pickups | Second native acceptance test |
| Old optional-field command fingerprints remain replay compatible | `tests/unit/test_replacement_replay.py` |

The migration assertion excludes extension-owned objects through PostgreSQL catalog
ownership, not a broad application-table/index filter. The two pre-existing application
index differences remain explicitly checked:
`ix_collection_request_pickup_location_gist` and
`ix_collection_request_planning_batch_id`. PaymentAttempt defaults and all three
initiation-term triggers remain checked by the shared assertion. The focused manager
migration test found no new schema drift.

## Limits and operational requirements

- Search is bounded and non-exhaustive. Full required regional work remains in the
  witness; exceeding pre-matrix node/vehicle/profile bounds returns UNKNOWN. Source
  capture is still proportional to regional work. No new scale benchmark is claimed.
- Native heuristics do not establish global optimality. Persisted ranking is stable;
  fresh runs may discover different validated witnesses. Costs and unavailable
  incremental baselines are reported explicitly.
- Source generations, expiry, elapsed-time checks and actual activation fence
  recommendations. A busy/changing region may require another evaluation; accepted
  bookings remain pending recovery rather than being silently dropped.
- An unresolved physical transfer must be completed/aborted through existing authority
  before group reevaluation. Per-pair transfer serialization requires sequential,
  individually evidenced transfers. A partial cart transfer retains each actual fact.
- Azure credentials, production media policy, catalogue, receiving/region policies,
  eligible shifts and a configured active fee remain deployment prerequisites.
  SAS permissions were verified through SDK/unit tests; no live Azure storage request
  or paid service was provisioned.
- Pre-migration history has explicitly unknown actor/command attribution. Adopted
  append-only history prevents an automatic lossy downgrade. No rollback UI, history
  deletion/retention redesign, push notification or frontend screen is included.
- V2 production activation flags are unchanged. Manager selection is advisory until
  normal transactional assignment/route authority succeeds. Custody changes only
  through existing physical transfer and handover authority.

## Changed-file manifest

This increment changes 70 files (including this report).

```text
docs/ARCHITECTURE.md
docs/DOMAIN_MODEL.md
docs/ER_DIAGRAM.md
docs/IDEMPOTENCY.md
docs/MANAGER_BUSINESS_CONFIGURATION.md
docs/MANAGER_BUSINESS_CONFIGURATION_INVENTORY.md
docs/MANAGER_BUSINESS_CONFIGURATION_VALIDATION.md
docs/PROJECT_CONTEXT.md
docs/SCHEMA_DESIGN.md
docs/adr/ADR-003-persistence.md
docs/adr/ADR-004-planning-compaction.md
docs/adr/ADR-006-rider-assignment.md
docs/adr/ADR-014-idempotency-reliability.md
migrations/env.py
migrations/versions/0033_manager_business_history.py
migrations/versions/0034_replacement_options.py
src/tirodhan/api/dependencies.py
src/tirodhan/api/router.py
src/tirodhan/api/routes/manager_business.py
src/tirodhan/api/routes/routing_orchestration.py
src/tirodhan/modules/business_admin/__init__.py
src/tirodhan/modules/business_admin/commands.py
src/tirodhan/modules/business_admin/events.py
src/tirodhan/modules/business_admin/fact_metadata.py
src/tirodhan/modules/business_admin/history.py
src/tirodhan/modules/business_admin/lineage.py
src/tirodhan/modules/business_admin/models.py
src/tirodhan/modules/business_admin/pricing.py
src/tirodhan/modules/business_admin/service.py
src/tirodhan/modules/dispatch/fleet_service.py
src/tirodhan/modules/dispatch/service.py
src/tirodhan/modules/evidence/azure_media.py
src/tirodhan/modules/operations/service.py
src/tirodhan/modules/routing_operations/authority.py
src/tirodhan/modules/routing_operations/capacity_models.py
src/tirodhan/modules/routing_operations/commands.py
src/tirodhan/modules/routing_operations/fleet_models.py
src/tirodhan/modules/routing_operations/models.py
src/tirodhan/modules/routing_operations/operational_commands.py
src/tirodhan/modules/routing_operations/orchestration_models.py
src/tirodhan/modules/routing_operations/parallel_models.py
src/tirodhan/modules/routing_operations/recovery.py
src/tirodhan/modules/routing_operations/regional_planner.py
src/tirodhan/modules/routing_operations/replacement_commands.py
src/tirodhan/modules/routing_operations/replacement_models.py
src/tirodhan/modules/routing_operations/replacement_options.py
src/tirodhan/modules/routing_operations/replacement_reads.py
src/tirodhan/modules/routing_operations/replacements.py
src/tirodhan/modules/routing_operations/rolling.py
tests/integration/legacy_financial_fixture.py
tests/integration/test_admission_reliability.py
tests/integration/test_admission_scalability.py
tests/integration/test_database.py
tests/integration/test_financial_correctness_closure.py
tests/integration/test_financial_reconciliation.py
tests/integration/test_financial_review_closure.py
tests/integration/test_manager_business.py
tests/integration/test_manager_migration.py
tests/integration/test_manager_replacement_native.py
tests/integration/test_manager_replacement_safety.py
tests/integration/test_orchestration_migration.py
tests/integration/test_regional_capacity_recovery.py
tests/integration/test_regional_parallel.py
tests/integration/test_regional_parallel_migration.py
tests/integration/test_routing_dispatch.py
tests/integration/test_routing_fleet.py
tests/integration/test_routing_operations.py
tests/unit/evidence/test_azure_media.py
tests/unit/test_replacement_gate.py
tests/unit/test_replacement_replay.py
```
