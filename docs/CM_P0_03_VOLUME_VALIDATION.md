# CM-P0-03 volume follow-up

22 October 2026. Base `2301d67938c4592b1e2174f5148090386136d947`, clean working tree;
implementation branch `codex/customer-item-volume`.

The existing catalogue and declarations have no physical dimensions or volume. Neither
weight nor category/quantity can uniquely supply occupied space. The minimal independent
input is one `declared_volume_ml` on each selected item line, covering its whole quantity
and packaging. The backend sums those declarations. This requires no dimensional shape
assumption, density, category mapping, envelope policy, configuration table or service.
The frontend must supply real declarations; this backend change adds no frontend UI.

For example, synthetic lines with quantities 2 and 3 and whole-line volumes 1000 ml and
2000 ml reserve a raw total of 3000 ml. Quantity does not multiply volume or declared
weight again. Weight alone continues to determine price.

The quote returns `total_volume_ml` and readiness. Price previews without complete volume
remain readable but cannot acquire a fresh customer hold. Complete declarations remove
`PHYSICAL_VOLUME_POLICY_REQUIRED`; mismatched caller aggregates are rejected. Volume joins
the quote, hold and booking fingerprints. Even redistributing an unchanged aggregate among
lines requires a new hold. Booking stores immutable line volumes; the manifest copies the
hold's existing mass and volume with the regional margin already applied exactly once.
Existing route validation still enforces each vehicle capacity dimension independently.

Migration `0038_item_volume` adds one nullable bigint column with positive/range checks and
deferred complete/bounded aggregate validation for volume-bearing weighted bookings. Existing
booking-item immutability covers the column. It leaves historical facts null and refuses
lossy downgrade when volume declarations exist. New code requires migration to head first.

The existing hold transaction owns idempotency claim, inventory locks, reservation, workload
debits, wake/outbox and result. The existing booking transaction owns items, request, Payment,
quote provenance, hold binding, manifest and result. Completed replay precedes current
configuration/expiry checks and adds neither inventory nor another booking. Omitted volume
preserves old declaration/command fingerprints. No new broker/provider side effect occurs.

## Verification

Tests use a task-owned disposable `postgis/postgis:17-3.5` database with no published ports;
the existing user database is untouched. The existing Python 3.13 Linux development image
runs database tests with repository source mounted read-only. Native Windows development
tools run Ruff and strict mypy. No push, merge, deployment or production activation.

| Run | Result | Scope |
| --- | --- | --- |
| Selected PostgreSQL/PostGIS integration regression | 261 passed, 18 warnings, 561.68 s | New volume flow plus existing pricing, booking recovery, holds, customer reads, payments, preparation/handoff, cancellation/refunds, completion, database, manager and migration suites |
| Final focused volume integration | 4 passed, 1 warning, 24.98 s | All volume flows plus retained pre-volume nested fingerprint and customer replay |
| Volume/weight arithmetic and routing validation | 106 passed, 6.72 s | Independent declared-volume validation, pricing arithmetic and hard mass/volume routing constraints |
| Ruff lint and formatting | Passed; 549 files formatted | Entire repository |
| Strict mypy | Passed; 253 source files | `--strict src` |
| Whitespace | Passed | Working diff |

The 261-case run included the first three volume integration cases. The subsequently added
pre-volume nested-fingerprint compatibility case is checked in the final focused run below.
No application or migration logic changed after the broad regression started. Its warnings
come from the existing reservation/location equality query's Cartesian-product warning;
the new volume flow exercises that same query once more.

Reproduction uses the existing `tirodhan-routing-tests:increment-8` image, repository mounted
at `/repo:ro`, `PYTHONPATH=/repo/src`, `TIRODHAN_HYPOTHESIS_DATABASE=/tmp/hypothesis` and
`TIRODHAN_TEST_DATABASE_URL` pointing solely to the task-owned disposable database.
Pytest runs with `-q --tb=short -p no:cacheprovider`. Focused suites:

```text
tests/integration/test_item_volume.py
tests/unit/test_item_volume_arithmetic.py
tests/unit/test_weight_price_arithmetic.py
tests/unit/test_routing_validation.py
```

The broad run additionally covers `test_weight_pricing`, `test_booking_recovery`,
`test_slot_holds`, `test_customer_mobile_core`, `test_collection_payment`,
`test_payment_preparation`, `test_checkout_handoff`, `test_cancellation_refunds`,
`test_customer_financial_cancellation`, `test_collection_request_completion`,
`test_database`, `test_manager_business`, `test_manager_migration`,
`test_orchestration_migration` and `test_regional_parallel_migration` in `tests/integration`.
Native local checks use `.venv/Scripts/python.exe -m ruff check .`,
`-m ruff format --check .`, and `-m mypy --strict src`.

New invariant coverage lives in `tests/integration/test_item_volume.py` and
`tests/unit/test_item_volume_arithmetic.py`: complete/missing/partial/invalid declarations,
whole-line sum and overflow, stale declaration binding with unchanged price/aggregate,
concurrent same-command holds, once-only safety margin, atomic rollback/retry, response-loss
booking recovery, immutable snapshots, retained pre-volume replay bytes and lossy rollback
refusal. Existing slot concurrency, financial, recovery and schema suites supply regression
coverage for unchanged safeguards.
