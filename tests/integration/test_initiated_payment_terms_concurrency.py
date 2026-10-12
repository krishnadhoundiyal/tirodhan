"""Freeze terms against the real FK lock, without relying on API pre-locks."""

import asyncio

import pytest
from razorpay_helpers import booking
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.payments.models import Payment, PaymentAttempt

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def wait_for_blocker(reader, waiting_pid, blocker_pid, task):
    # Prove a database lock wait, rather than relying on a sleep/task timing guess.
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        blockers = await reader.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid})
        if blocker_pid in blockers:
            assert not task.done()
            return True
        if task.done():
            return False
        await asyncio.sleep(0.01)
    raise AssertionError("second writer neither blocked on the first nor completed")


@pytest.mark.parametrize("field,value", [("amount_minor", 600), ("currency", "USD")])
@pytest.mark.parametrize("first", ["terms", "attempt"])
@pytest.mark.parametrize("commit_first", [True, False])
async def test_first_attempt_and_terms_update_serialize(
    database_session_factory, field, value, first, commit_first
):
    factory = database_session_factory
    _, result = await booking(factory)
    payment_id, attempt_id = result.payment.payment_id, new_uuid7()
    old_value = getattr(result.payment, field)
    attempt_insert = insert(PaymentAttempt).values(
        payment_attempt_id=attempt_id,
        payment_id=payment_id,
        provider="RAZORPAY",
        provider_idempotency_key=f"payment-attempt:{attempt_id}",
        status="CREATED",
        created_at=utc_now(),
    )
    # A non-key UPDATE normally takes FOR NO KEY UPDATE, while this INSERT's
    # actual FK takes FOR KEY SHARE. Do not add an application FOR UPDATE here.
    terms_update = text(f"UPDATE payment SET {field}=:value WHERE payment_id=:id")

    async with factory() as a, factory() as b:
        for session in (a, b):
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            await session.execute(text("SET LOCAL statement_timeout='8s'"))
        pid_a, pid_b = (
            await a.scalar(text("SELECT pg_backend_pid()")),
            await b.scalar(text("SELECT pg_backend_pid()")),
        )
        assert pid_a != pid_b  # Two genuinely distinct PostgreSQL sessions.

        async def update(session):
            await session.execute(terms_update, {"value": value, "id": payment_id})

        async def insert_attempt(session):
            await session.execute(attempt_insert)

        first_operation = update if first == "terms" else insert_attempt
        second_operation = insert_attempt if first == "terms" else update
        await first_operation(a)
        waiting = asyncio.create_task(second_operation(b))
        try:
            blocked = await wait_for_blocker(a, pid_b, pid_a, waiting)
            if commit_first:
                await a.commit()
            else:
                await a.rollback()
            if first == "attempt" and commit_first and blocked:
                with pytest.raises(IntegrityError, match="Initiated payment terms are immutable"):
                    await asyncio.wait_for(waiting, 5)
                await b.rollback()
            else:
                await asyncio.wait_for(waiting, 5)
                await b.commit()
        finally:
            # Keep assertion/error paths from leaving blocked writers or open txs.
            await a.rollback()
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            await b.rollback()

        actual_value = await b.scalar(
            select(getattr(Payment, field)).where(Payment.payment_id == payment_id)
        )
        attempt_count = await b.scalar(
            select(func.count())
            .select_from(PaymentAttempt)
            .where(PaymentAttempt.payment_id == payment_id)
        )
        # On vulnerable 0023 both commit: first terms UPDATE uncommitted, then FK
        # INSERT commits, then terms UPDATE commits after durable initiation.
        assert blocked, (
            f"compatible locks allowed both writers to commit: {first=}, "
            f"{actual_value=}, {attempt_count=}, {commit_first=}"
        )
        expected_value = value if first == "terms" and commit_first else old_value
        if first == "attempt" and not commit_first:
            expected_value = value
        assert actual_value == expected_value
        assert attempt_count == (1 if first == "terms" or commit_first else 0)
        if attempt_count:
            # Exact frozen-value replay succeeds; a later changed-value retry fails.
            await b.execute(terms_update, {"value": actual_value, "id": payment_id})
            await b.commit()
            changed = value if actual_value == old_value else old_value
            with pytest.raises(IntegrityError, match="Initiated payment terms are immutable"):
                await b.execute(terms_update, {"value": changed, "id": payment_id})
            await b.rollback()
