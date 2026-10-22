# CM-P0-02 validation report

Date: 22 October 2026. Branch: `codex/cm-p0-02-booking-recovery`.
Verified implementation base: clean `main`,
`c1e2e7c7fba12bda3b3df03710418c2f1e7b0d0b`, merged PR #68.
`Get-Location` returned `C:\Users\91956\Tirodhan\tirodhan`; initial working-tree
status was empty. The existing projected-slot hold implementation is present at base.

## Final focused PostgreSQL/PostGIS run

**231 passed, 0 failed, 0 skipped, 16 warnings in 469.39 seconds.**
All 15 new booking-recovery cases passed in this final invocation.

The task created its own local `postgis/postgis:17-3.5` container with 2 CPUs / 1 GiB,
no published ports, and the disposable `tirodhan_test` database. Tests reused the existing
Linux image `tirodhan-routing-tests:increment-8` (Python 3.13.5), mounting current source
read-only. Existing user databases/containers were not used or modified. Alembic upgraded
the disposable database; existing migration round trips and schema assertions ran there.
No application schema/migration changed in this increment.

Executed from the repository in PowerShell:

```powershell
docker run -d --name tirodhan-cm-p0-02-db --cpus 2 --memory 1g `
  -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=tirodhan_test postgis/postgis:17-3.5
docker exec tirodhan-cm-p0-02-db pg_isready -U postgres -d tirodhan_test

docker run --rm --cpus 2 --memory 4g --network container:tirodhan-cm-p0-02-db `
  -v "${PWD}:/repo:ro" -w /repo -e PYTHONPATH=/repo/src `
  -e TIRODHAN_HYPOTHESIS_DATABASE=/tmp/hypothesis `
  -e TIRODHAN_TEST_DATABASE_URL=postgresql+asyncpg://postgres:local-test-only@127.0.0.1:5432/tirodhan_test `
  tirodhan-routing-tests:increment-8 python -m pytest `
  tests/integration/test_booking_recovery.py `
  tests/integration/test_slot_holds.py `
  tests/integration/test_customer_mobile_core.py `
  tests/integration/test_collection_payment.py `
  tests/integration/test_payment_preparation.py `
  tests/integration/test_checkout_handoff.py `
  tests/integration/test_cancellation_refunds.py `
  tests/integration/test_customer_financial_cancellation.py `
  tests/integration/test_collection_request_completion.py `
  tests/integration/test_database.py -q --tb=short -p no:cacheprovider
```

The warnings are the existing SQLAlchemy PK-filtered cartesian-product warning in
reservation/location comparison (12 from recovery tests, 4 from existing holds). That
comparison query is unchanged. No warning filter or test exclusion was introduced.

### New recovery coverage

`tests/integration/test_booking_recovery.py` covers:

- Exact response allow-list; unbound JSON null; missing/foreign 404 equivalence; unauthenticated refusal.
- Real HTTP transport response loss after booking commit, fresh app/client/session recovery
  using the original reservation identity, owner-protected booking detail and bound hold replay.
- No new financial acceptance: recovered booking remains PENDING_PAYMENT with PENDING Payment,
  no successful attempt and no extra payment attempt created by recovery.
- Binding/replay after `book_by`, reservation expiry and original slot passage, with current
  pricing unavailable and fresh slot validation forbidden; changed-body replay still conflicts.
- Unbound expired/unbookable holds never invent a booking.
- Binding after actual customer cancellation and verified synthetic payment capture; binding
  after a persisted COMPLETED fixture, plus separate existing completion/evidence regression tests.
- Competing identical and different booking commands, with the existing overlap harness
  confirming **both live PostgreSQL lock waits** before releasing a reservation row blocker.
  Same-key submissions converge; different keys leave one booking/Payment/manifest.
- Explicit rejection of another request consuming an already-bound reservation.
- Crash injected by SQLAlchemy **after execution of the actual reservation UPDATE**, before
  transaction commit; rollback removes request/Payment/manifest/command result and binding.
  Identical retry subsequently succeeds.
- A transaction paused after flushed binding but before commit: an independent reader sees null,
  then sees the committed ID after release. A null snapshot cannot establish that an uncertain
  booking submission failed.
- Insufficient reservation lifetime rolls back fresh booking without false binding; corrected
  retry retains the same logical booking key.

The completed-state recovery test intentionally seeds terminal fulfilment state rather than
re-enacting a complete routed pickup/custody workflow. The existing completion suite independently
tests authorization prerequisites, evidence and completed replay. Financial events in tests are
authenticated synthetic inputs; no live gateway charge is performed.

### Test development iterations

The initial focused recovery-only run returned 8 passed / 5 failed (42.68 seconds): five
new count assertions incorrectly counted the shared fixture's two existing bookings as new
effects. Assertions now compare against the fixture baseline.

The next recovery-only run returned 14 passed / 1 failed (40.87 seconds): the crash test
assumed ORM flush would update the reservation before inserting the manifest. Failure injection
now hooks the reservation UPDATE's actual completion, proving that the intended DB write occurred.
Both were test-harness corrections; the final 231-case invocation above passes the final test file.

## Wider unit and routing-contract run

**638 passed, 2 failed, 5 skipped, 23 deselected, 5 warnings in 60.71 seconds.**
This run is not reported as green. Executed:

```powershell
docker run --rm --cpus 2 --memory 4g `
  -v "${PWD}:/repo:ro" -w /repo -e PYTHONPATH=/repo/src `
  -e TIRODHAN_HYPOTHESIS_DATABASE=/tmp/hypothesis `
  tirodhan-routing-tests:increment-8 python -m pytest tests/unit tests/routing `
  -m 'not routing_engine' -q --tb=short -p no:cacheprovider
```

Failures in unchanged files:

1. `tests/unit/test_dispatch_push.py::test_fcm_real_sdk_multicast_chunks_start_concurrently`:
   the two concurrent SDK calls began in order `[1, 500]`, while the assertion requires
   `[500, 1]`. The source/test are identical to the base. This scheduling-sensitive assertion
   passed in isolation on the base; no notification code was changed or assertion weakened.
2. `tests/unit/test_twofactor.py::test_timeout_traceback_and_http_logging_hide_sensitive_url`:
   the traceback assertion fails on Python 3.13.5. It reproduces against an archive of
   the original base in the same image (1 passed / 1 failed, 11.87 seconds for these two tests).
   The unchanged runtime/test incompatibility is already recorded in
   [PROJECTED_SLOT_ALLOWANCE_VALIDATION.md](PROJECTED_SLOT_ALLOWANCE_VALIDATION.md).

The clean-base comparison used `git archive HEAD` before committing any changes, exporting
`src`, the two unit test files, `tests/conftest.py` and `pyproject.toml` into ignored
`.tools/cm-p0-02-baseline`; that directory was mounted read-only as `/repo` in the same
image, with the two fully qualified pytest node IDs above. The working checkout was not
reset or modified to perform the comparison.

An additional attempt to run those two comparison tests on host Python 3.10.5 produced
no output and was interrupted; it supplies no passing result. The clean-base Linux
comparison above is the executed failure-isolation evidence for this increment.

The five skips require explicit local OSRM graph/native-engine/runtime opt-in (two OSRM
quantization tests, one regional runtime test and two real-engine selection cases).
The 23 deselections are the requested `routing_engine` marker exclusion. Warnings were four
existing Firebase SDK `MulticastMessage.tokens` deprecations and one unawaited ByteStream
close warning in an unchanged financial-provider boundary test. Native graph/benchmark
campaigns and the complete repository integration suite were not executed in this increment.

## Static checks

Executed on the host Python 3.10.5 environment:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy --strict src
git diff --check
```

Ruff lint passed; Ruff format passed (536 files); strict mypy passed (250 source files).
No lint/type exclusions were added. The staged diff is checked again before commit so new
documentation and tests are included in whitespace validation.

## Scope and limits

The sole runtime source change is the additive owner-protected `bound_request_id` projection
in `src/tirodhan/api/routes/routing_orchestration.py`. `bookable`, deadlines, source/generation
freshness and all mutation transactions retain their existing behavior. There is no extra
lookup service, provider call, worker, queue, reconciliation mechanism or customer measurement.

Payment-first acceptance, existing provider financial authority, operational vehicle-capacity
validation, route authority, inbox/outbox and evidenced physical custody are unchanged.
No Azure resource, production flag, frontend source, deployment, push or merge is part of this
increment. The local disposable database is removed after validation.

The physical-load document is a proposal for explicit approval and later implementation.
Neither field measurements nor production envelope calibration is established by these tests.
Customer Mobile live hold submission remains gated on an approved and implemented physical-load
source. See [handoff](CM_P0_02_BOOKING_RECOVERY.md) and
[policy proposal](PHYSICAL_LOAD_POLICY_PROPOSAL.md).
