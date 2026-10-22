# CM-P0-03 validation and delivery record

22 October 2026. Working directory verified as `C:\Users\91956\Tirodhan\tirodhan`.
Initial branch `codex/cm-p0-02-booking-recovery`, HEAD
`bdf13021c8c1c19819a507926b25e7661e8b81e2`, clean working tree.
Implementation branch: `codex/cm-p0-03-weight-pricing`, directly retaining CM-P0-02.

The [API, manager configuration, mathematical specification, frontend handoff and volume
gate](CM_P0_03_WEIGHT_PRICING.md) describe the implemented behavior. No production values
are seeded, no frontend is modified, and no push, merge or deployment is performed.

## Executed results

| Run | Result | Scope / limits |
| --- | --- | --- |
| Clean-database selected integration regression | **258 passed, 0 failed, 0 skipped**, 17 warnings, 523.69 s | Booking/hold/recovery, new pricing, customer reads, payment/preparation/handoff, cancellation/refunds, completion, manager and migration preservation suites |
| Final focused arithmetic and weight-pricing contract | **53 passed, 0 failed, 0 skipped**, 1 warning, 49.19 s | 43 arithmetic/declaration cases and 10 integration cases; includes the final quote input policy/version check and all reservation-provenance downgrade guards |
| Wider unit + routing contracts | **682 passed, 1 failed, 5 skipped, 23 deselected**, 5 warnings, 65.10 s | All unit and non-native routing suites; unchanged OTP traceback failure below |
| Ruff lint | Passed | Entire repository; no exclusions added |
| Ruff formatting | Passed | 544 files checked |
| Strict mypy | Passed | 253 source files |
| Whitespace | Passed | Working diff; staged diff checked before commit |

The clean 258-case invocation preceded the final quote-request policy/version guard and
the added conservative downgrade check for a lone quote fingerprint. The final 53-case
invocation verifies the final affected pricing/API/migration tests after those refinements.
This is evidence from the recorded runs, not a claim that the entire repository/native suite
was rerun as one green invocation on the final tree.

Database tests used a task-owned disposable `postgis/postgis:17-3.5` container, 2 CPUs / 1 GiB,
without published ports, and `tirodhan_test`. The existing local Linux image
`tirodhan-routing-tests:increment-8` (Python 3.13.5) ran application tests with source mounted
read-only, 2 CPUs / 4 GiB. The database was rebuilt from scratch for the clean integration run
and is removed after validation. Existing user databases/containers and Azure were untouched.

## Commands

From the repository in PowerShell, start the disposable database and verify readiness:

```powershell
docker run -d --name tirodhan-cm-p0-03-db --cpus 2 --memory 1g `
  -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=tirodhan_test postgis/postgis:17-3.5
docker exec tirodhan-cm-p0-03-db pg_isready -U postgres -d tirodhan_test
```

Clean selected integration regression:

```powershell
docker run --rm --cpus 2 --memory 4g --network container:tirodhan-cm-p0-03-db `
  -v "${PWD}:/repo:ro" -w /repo -e PYTHONPATH=/repo/src `
  -e TIRODHAN_HYPOTHESIS_DATABASE=/tmp/hypothesis `
  -e TIRODHAN_TEST_DATABASE_URL=postgresql+asyncpg://postgres:local-test-only@127.0.0.1:5432/tirodhan_test `
  tirodhan-routing-tests:increment-8 python -m pytest `
  tests/integration/test_weight_pricing.py `
  tests/integration/test_booking_recovery.py `
  tests/integration/test_slot_holds.py `
  tests/integration/test_customer_mobile_core.py `
  tests/integration/test_collection_payment.py `
  tests/integration/test_payment_preparation.py `
  tests/integration/test_checkout_handoff.py `
  tests/integration/test_cancellation_refunds.py `
  tests/integration/test_customer_financial_cancellation.py `
  tests/integration/test_collection_request_completion.py `
  tests/integration/test_database.py `
  tests/integration/test_manager_business.py `
  tests/integration/test_manager_migration.py `
  tests/integration/test_orchestration_migration.py `
  tests/integration/test_regional_parallel_migration.py -q --tb=short -p no:cacheprovider
```

The final focused invocation used the same Docker options and database, selecting only
`tests/unit/test_weight_price_arithmetic.py tests/integration/test_weight_pricing.py`
with `-q --tb=short -p no:cacheprovider`.

Wider run:

```powershell
docker run --rm --cpus 2 --memory 4g -v "${PWD}:/repo:ro" -w /repo `
  -e PYTHONPATH=/repo/src -e TIRODHAN_HYPOTHESIS_DATABASE=/tmp/hypothesis `
  tirodhan-routing-tests:increment-8 python -m pytest tests/unit tests/routing `
  -m 'not routing_engine' -q --tb=short -p no:cacheprovider
```

Host static checks (Python 3.10.5):

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy --strict src
git diff --check
git diff --cached --check
```

## Invariants demonstrated

New arithmetic/declaration tests cover required configuration, B/T ordering, missing bands,
finite upper boundaries, strictly-above-T numerical input, total-quantity semantics, multiple
lines, exact gram conversion, malformed/unsupported precision, numeric/aggregate overflow,
X/Y saturation, equal X/Y, exact half-up ties, monotonic bounded rounded output and generated
diminishing marginal rate on the underlying exact curve. They also verify legacy item repr
compatibility for existing booking fingerprints. Fixtures are explicitly synthetic.

New PostgreSQL integration coverage demonstrates:

- Manager exact replay, expected-revision conflicts, real overlapping PostgreSQL lock waits
  for competing updates, responsible actor/command history, exact decimal history reads and
  SQL append-only protection.
- Quote display creates no booking, Payment, reservation or manifest; repeated query is stable,
  expected policy/version is checked, caller price overrides are rejected, and retirement
  produces unavailable weight pricing rather than flat fallback.
- A draft leaves legacy pricing available; first activation is sticky. Confirmed weight-priced
  HTTP booking requires a hold even when orchestration registration is disabled.
- Stale confirmation fails without a booking. Explicit events pause a quote outside the final
  transaction while a manager update commits; the final configuration fence rejects it.
- Booking/Payment retain the original price and original raw item weights. SQL rejects edits
  before payment initiation. Verified synthetic capture, later physical observation, a newer
  pricing revision and cancellation/refund preserve the original monetary amount.
- Trusted operational fixture holds use the same aggregate, apply the existing regional margin
  once, and copy reserved mass/volume unchanged into the manifest. Changed quantities with
  unchanged aggregate grams cannot bind the hold. Fresh public holds reject supplied volume
  while completed hold replay remains available.
- Weighted booking response loss after commit recovers through bound_request_id and owner
  detail, with exact booking replay and original pricing provenance.
- Pre-transition legacy booking replay works after weight-policy activation.
- Migration 0037 empty-feature round trip and populated-history downgrade refusal; existing
  strict schema comparison and financial-history preservation checks remain effective.

The wider run includes existing `test_routing_validation.py::test_each_capacity_dimension_is_hard`
for both mass and volume, along with cargo conservation and other independent-validator tests.
No operational capacity check, financial acceptance rule or custody implementation was weakened.
The trusted hold fixture does not establish an approved customer volume source or real fleet
calibration. All payment/refund provider responses in tests are synthetic; no real charge occurs.

## Failures, warnings and limitations

The wider run is not green:
`tests/unit/test_twofactor.py::test_timeout_traceback_and_http_logging_hide_sensitive_url`
fails on Python 3.13.5. It is the unchanged traceback/source assertion reproduced on the base
in [CM-P0-02 validation](CM_P0_02_VALIDATION.md). The relevant OTP source/test are unchanged
from `bdf13021c8c1c19819a507926b25e7661e8b81e2`; no identity code or assertion was modified.
The previously intermittent FCM ordering test passed in this invocation.

The five skips require explicit local OSRM/native runtime opt-in: two quantization cases,
one regional runtime case and two real-engine selection cases. The 23 deselections are the
existing `routing_engine` marker selection. Native graph/benchmark campaigns, full repository
integration, live mobile, live provider behavior and production calibration were not run.

Integration warnings are the unchanged PK-filtered reservation/location cartesian-product
SQLAlchemy warning (12 recovery, 4 existing holds, 1 new pricing integration). Wider warnings
are four existing Firebase token-property deprecations and one ByteStream close warning in an
unchanged provider-boundary test. No warning suppression was introduced.

Development runs are retained here for accuracy: an early arithmetic/recovery/manager smoke
run passed 68 cases (86.68 s). The first new integration run returned 6 passed / 2 failed
(27.10 s), due to expecting “immutable” where the reused SQL history guard says “append-only”;
the tests now match both supported guards. An intermediate focused run passed 52 cases
(41.56 s) before the final legacy replay/API refinements.

The first expanded regression run returned 236 passed / 16 failed (489.15 s): two old
financial snapshots differed only by the newly added null item keys; fourteen later migration
cases hit a function/schema mismatch after the uncommitted migration was edited during that
run. That database was discarded. The clean 258-case run above rebuilt the final migration
and passed. Historical snapshots now **assert null** before omitting the two unavailable
pre-0037 fields; populated weight facts are not discarded or exempted. Historical financial
fixture writes use only old-schema columns rather than new runtime models against old schema.
No schema-comparison exclusions or original financial assertions were removed.

## Affected files

New runtime source:

- `src/tirodhan/modules/business_admin/weight_contracts.py`
- `src/tirodhan/modules/business_admin/weight_pricing.py`
- `src/tirodhan/api/routes/customer_weight_pricing.py`

Extended existing runtime source:

- `src/tirodhan/api/dependencies.py`, `src/tirodhan/api/router.py`
- `src/tirodhan/api/routes/collection_requests.py`, `src/tirodhan/api/routes/routing_orchestration.py`
- `src/tirodhan/modules/business_admin/commands.py`, `models.py`, `service.py`
- `src/tirodhan/modules/collection_requests/models.py`, `ports.py`, `service.py`
- `src/tirodhan/modules/customer_reads/repository.py`, `schemas.py`
- `src/tirodhan/modules/routing_operations/admission.py`, `orchestration_models.py`, `slot_holds.py`

Migration: `migrations/versions/0037_weight_pricing.py` (no older migration modified).

Tests: new `tests/unit/test_weight_price_arithmetic.py` and
`tests/integration/test_weight_pricing.py`; adapted existing `legacy_financial_fixture.py`,
`test_manager_migration.py`, `test_payment_preparation.py` and `test_slot_holds.py` under
`tests/integration` for historical schema fixtures/nullable provenance.

Documentation: this report, the CM-P0-03 contract, PROJECT_CONTEXT, ARCHITECTURE, DOMAIN_MODEL,
SCHEMA_DESIGN, ER_DIAGRAM, IDEMPOTENCY, ADR-005 and the superseded mass-policy proposal notice.

## Remaining release gate

Physical volume still needs an independently approved source, numeric calibration, eligibility,
versioned provenance and mismatch SOP. Fresh weight-priced customer hold POSTs remain blocked
even with caller-supplied positive volume; no substitute density, item-name estimate, placeholder
or production bypass flag is provided. Configurable prices/bands also require actual manager
configuration; none is a production default. Existing payment-first acceptance, provider amount
verification, refund independence, operational route validation, evidenced custody and production
flags remain unchanged.
