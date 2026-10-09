# Phase 2 operational pickup-slot availability correction

Implementation date: 2026-10-09. This is the approved scheduling follow-up to
Batch A, on `phase2/customer-mobile-core-apis`, based on
`2634ecb62294ce1202925da37db006cca7bd83b0`. Preflight verified that exact HEAD,
the existing branch, and a clean worktree. The correction's local commit SHA is
reported in the delivery message. Nothing is pushed, merged, deployed, or opened as a PR.

## Approved policy and runtime wiring

`DelhiSlotAvailability` is now the production default in both `create_app` and
`create_collection_request`. The existing `SlotAvailabilityPort` is unchanged,
and explicit dependency injection remains supported. `UnconfiguredSlotAvailability`
remains available for explicit failure tests, rather than being the runtime default.

The constants in the existing scheduling module implement precisely the approved
Delhi launch policy: seven calendar dates including today in `Asia/Kolkata`, with
an inclusive final date six days later; operating hours 06:00–22:00; and consecutive
30-minute intervals. `daily_grid` retains all 48 underlying intervals; `operating_grid`
selects its 32 operating intervals, first 06:00–06:30 and last 21:30–22:00.
No immediate/ASAP pickup is offered. Current time comes from the backend.

The concrete port adds deterministic horizon and hours eligibility. The existing
caller checks compose it with authenticated customer ownership, PostgreSQL
SERVICEABLE context/location/cell/expiry, configured planning cutoff, and persisted
freeze state. No coverage cells are generated or expanded. No provider call, rider
quota, fleet inference, capacity reservation, or manufactured FULL state is added.
AVAILABLE expresses booking eligibility at evaluation time; dispatch and rider
allocation retain their existing independent architecture.

## Slot read and query strategy

`GET /v1/customer/pickup-slots?serviceability_context_id=...` retains its DTO.
The existing live CUSTOMER authorization and read-only repeatable-read transaction
remain. Foreign/missing contexts fail without disclosing ownership; expired,
unserviceable, unresolved, or incomplete contexts cannot produce available slots.
Missing planning lead-time configuration fails with 503 even if no candidates remain.
Database failure prevents a successful availability response.

The API computes at most 224 operating windows under the production policy, filters
past/cutoff windows in memory using the existing configured lead time, then performs
one bounded query for exact `(slot_start, slot_end)` planning windows in the context's
resolved cell. Any matching batch freezes the slot regardless of batch lifecycle
status. The existing unique cell/start/end index supports this lookup. No per-slot
SQL occurs: the concrete port's per-window calculations are in memory. With no
candidates, the planning query is omitted and the slot array is empty.

Responses retain deterministic local IDs and display labels, UTC timestamps,
context expiry, and private/no-store caching. The existing FULL DTO alternative is
retained only for explicitly supplied policies. Read responses do not reserve work.

## Booking revalidation and concurrency

`POST /v1/collection-requests` still takes `slot_start` and `slot_end`, without a
new slot ID. Fresh commands independently validate aware instants, exact duration,
local :00/:30 alignment, future start, ownership, SERVICEABLE state, expiry, resolved
point/cell, seven-date horizon, approved hours, configured cutoff, and freeze state.
PENDING serviceability continues using the established shared resolution operation
outside the final booking transaction. Pricing remains outside that transaction.

The existing `(cell_id, slot_start, slot_end)` transaction-scoped PostgreSQL advisory
lock is unchanged. After obtaining it, booking recalculates current time, cutoff,
horizon and port eligibility, queries authoritative freeze state, and rechecks context
and payment expiry before writing. Distinct valid contexts may concurrently book the
same cell/slot; no new quota or unrelated lock is introduced. The existing unique
serviceability-context constraint still permits only one booking per context.

Command idempotency remains `(collection-request.create:<customer_id>, client_request_id)`
with the existing request fingerprint and `(customer_id, client_request_id)` uniqueness.
Request, items, logical payment and completed idempotency result commit atomically.
Exact completed replay returns its durable resource before fresh eligibility/pricing
checks, including after slot expiry, horizon movement, context expiry, or freeze.
Different input under a committed key conflicts. Rejection rolls back the command
claim and business rows. No new external side effect, inbox, or outbox route is added.

## Verification

Focused tests exercise seven dates, all 32 intervals and boundaries, exact UTC
serialization, IDs/labels, local midnight/year changes, horizon expiry, past/cutoff
boundaries using a test-configured 17-minute lead, context ownership/state/expiry,
live customer-role checks, missing configuration, and empty results.

Real PostgreSQL tests cover independent invalid booking rejection, multiple concurrent
same-cell/slot bookings using distinct contexts, exact concurrent replay, replay after
eligibility changes, actual advisory-lock waits crossing cutoff/context expiry/date,
and the real `freeze_planning_batch` command winning against a blocked fresh booking.
Existing booking rollback/retry and serviceability/provider-boundary regressions remain.

An SQLAlchemy engine listener counts the real API statements for the complete 224-slot
response: three statements with the test principal dependency (read-only transaction
configuration, owned-context SELECT, one planning SELECT), identical on repeated reads
and independent of client-supplied device dates. Live authorization is tested separately.
No per-candidate database query is issued.

Validation commands:

```powershell
$env:TIRODHAN_TEST_DATABASE_URL='postgresql+asyncpg://tirodhan:tirodhan@localhost:5432/tirodhan_batch_a_test'
.\.venv\Scripts\python.exe -m pytest -q --tb=short
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
git diff --check
```

The database is the existing disposable local PostgreSQL 17/PostGIS 3.5 environment;
provider regressions use their established test adapters rather than live paid calls.
The complete suite includes affected collection/payment, planning foundation/result,
serviceability runtime/context, Customer Mobile, and migration regressions.

Final results:

| Check | Result |
|---|---|
| New scheduling unit tests | 25 passed in the complete run |
| New scheduling PostgreSQL integration tests | 27 passed in the complete run |
| All PostgreSQL integration tests | 406 passed |
| All non-integration tests | 413 passed, 1 skipped |
| Complete backend suite | **819 passed, 1 skipped**, 4 existing warnings, 486.35 seconds |
| Ruff lint | Passed |
| Ruff formatting | 261 files passed |
| Mypy | 136 source files passed |
| Diff whitespace check | Passed |
| Alembic head | `0018_customer_mobile_core`; no new migration |

The skip is the POSIX signal exit-status test in `tests/unit/test_deployment_job.py:99`,
which is inapplicable on Windows. Four existing Firebase Admin SDK deprecation warnings
originate from the unchanged dispatch adapter. The earlier focused run passed 67 tests
(25 new unit, 26 new PostgreSQL, 16 existing Customer Mobile); the final additional HTTP
read-to-booking case passed in the complete suite. No live provider integration or
deployment validation is claimed.

## Files changed and scope

- `.env.example`: documents the approved default and existing lead-time authority.
- `src/tirodhan/modules/collection_requests/scheduling.py`: concrete approved policy,
  constants and operating-grid filter; existing validation/lock path retained.
- `src/tirodhan/modules/collection_requests/service.py`: production policy default.
- `src/tirodhan/main.py`: application policy default.
- `src/tirodhan/api/routes/customer_reads.py`: operating candidates, cutoff filtering,
  exact bounded freeze query.
- `tests/unit/test_delhi_slot_policy.py`: deterministic policy boundary tests.
- `tests/integration/test_delhi_slot_availability.py`: PostgreSQL API, concurrency,
  revalidation and bounded-query tests.
- `tests/integration/test_customer_mobile_core.py`: explicit unavailable-policy injection
  for existing failure tests and the approved 32-window read expectation.
- `docs/PHASE_2_BATCH_A_REPORT.md`: points to this follow-up for current scheduling status.
- `docs/PHASE_2_SLOT_AVAILABILITY_REPORT.md`: this correction report.

No new migration: Alembic remains at `0018_customer_mobile_core`.
Frontend baseline is clean `main` at `103d181eae4ebc539f8253cb009ac1b42c953e12`.
Frontend, Terraform, deployment manifests, infrastructure, and broader domain algorithms
are unchanged. No new dependency, paid service, queue, scheduler or cache is introduced.
No unapproved business policy is added. Existing independently unconfigured pricing and
other Batch A commercial/display decisions are outside this correction's scope.
There is no remaining scheduling-policy blocker. Runtime PostgreSQL connectivity,
live authorization, valid resolved serviceability, configured planning lead time and
existing booking/pricing configuration remain prerequisites; no fallback invents them.
