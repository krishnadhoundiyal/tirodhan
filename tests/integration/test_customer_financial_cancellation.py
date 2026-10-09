import asyncio
import hashlib
from datetime import timedelta

import httpx
import pytest
from razorpay_helpers import Orders, booking, initiate, payment_body, settings, signed
from sqlalchemy import event as sql_event
from sqlalchemy import func, select, text, update
from test_checkout_handoff import financial_snapshot
from test_customer_mobile_core import app_for
from test_operational_api import _token_for
from test_razorpay_refunds import Refunds, message

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests import cancellation
from tirodhan.modules.collection_requests.cancellation import CancellationConflictError
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.identity.models import RefreshSession, UserRole
from tirodhan.modules.payments import service
from tirodhan.modules.payments.models import Payment, PaymentAttempt, PaymentProviderEvent, Refund
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.payments.refunds import RefundConflictError, create_refund
from tirodhan.modules.planning import service as planning
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.reliability.models import IdempotencyRecord, OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def cancel(factory, request, key="cancel", label=None):
    async with factory() as session, session.begin():
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
        if label:
            await session.execute(
                text("SELECT set_config('application_name', :label, true)"), {"label": label}
            )
        return await cancellation.cancel_collection_request_by_customer(
            session,
            request_id=request.request_id,
            customer_id=request.customer_id,
            idempotency_key=key,
            planning_lead_time_minutes=30,
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )


async def capture(factory, provider, attempt, event_id="capture"):
    body = payment_body(attempt)
    event = await provider.authenticate_webhook(raw_body=body, headers=signed(body, event_id))
    return await service.process_authenticated_payment_event(
        factory,
        event,
        payload_hash=hashlib.sha256(body).digest(),
        planning_lead_time_minutes=30,
        idempotency_expires_at=utc_now() + timedelta(days=1),
    )


async def records(factory):
    async with factory() as session:
        return (
            list(await session.scalars(select(Refund))),
            list(
                await session.scalars(
                    select(OutboxEvent).where(OutboxEvent.event_type == "RefundRequested")
                )
            ),
        )


async def blocked(factory, label=None):
    async def wait():
        while True:
            async with factory() as session:
                waiting = await session.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE datname=current_database() AND wait_event_type='Lock' "
                        "AND cardinality(pg_blocking_pids(pid)) > 0 "
                        + ("AND application_name=:label)" if label else ")")
                    ),
                    {"label": label} if label else {},
                )
            if waiting:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), 5)


@pytest.mark.parametrize("captured", [False, True])
async def test_cancel_api_atomic_compensation_replay_and_matching_reads(
    database_session_factory, address_protector, captured
):
    factory = database_session_factory
    user, booking_result = await booking(factory)
    request = booking_result.request
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, booking_result, provider)
        if captured:
            await capture(factory, provider, attempt)
        async with factory() as session, session.begin():
            row = await session.get(CollectionRequest, request.request_id)
            row.pickup_address_snapshot_encrypted = await address_protector.protect(
                "Immutable booking"
            )
        app = app_for(factory, user.user_id, address_protector)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            base = f"/v1/customer/collection-requests/{request.request_id}"
            before = (await api.get(base)).json()
            assert before["cancellation"]["allowed"]
            assert before["cancellation"]["refund_expectation"] == (
                "FULL_PAYMENT" if captured else "NONE"
            )
            response = await api.post(
                f"/v1/collection-requests/{request.request_id}/cancel",
                headers={"Idempotency-Key": "one"},
            )
            assert response.status_code == 200 and response.json()["status"] == "CANCELLED"
            for key in ("one", "different"):
                assert (
                    await api.post(
                        f"/v1/collection-requests/{request.request_id}/cancel",
                        headers={"Idempotency-Key": key},
                    )
                ).json() == response.json()
            detail = (await api.get(base)).json()
            payment = await api.get(base + "/payment")
            refunds = await api.get(base + "/refunds")
            assert payment.json() == detail["payment"]
            assert refunds.json() == {"refunds": detail["refunds"]}
            assert payment.headers["cache-control"] == "private, no-store"
            assert refunds.headers["cache-control"] == "private, no-store"
            assert payment.json()["status"] == ("SUCCEEDED" if captured else "CANCELLED")
            assert not payment.json()["retry_allowed"]
            if captured:
                assert refunds.json()["refunds"][0]["status"] == "INITIATED"
                assert refunds.json()["refunds"][0]["amount_minor"] == 500
            else:
                assert refunds.json() == {"refunds": []}
            assert "provider_payment_id" not in refunds.text and "test-secret" not in payment.text
        refunded, events = await records(factory)
        assert len(refunded) == len(events) == int(captured)
        if captured:
            assert events[0].payload == {"refund_id": str(refunded[0].refund_id)}
            assert refunded[0].reason_code == "CUSTOMER_CANCELLATION"
            assert refunded[0].payment_attempt_id == attempt.payment_attempt_id


@pytest.mark.parametrize("same_key", [True, False])
async def test_competing_cancellation_commands_create_one_full_refund(
    database_session_factory, same_key
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        await capture(factory, provider, attempt)
        a, b = await asyncio.wait_for(
            asyncio.gather(
                cancel(factory, result.request, "one"),
                cancel(factory, result.request, "one" if same_key else "two"),
            ),
            10,
        )
    assert a.status == b.status == "CANCELLED"
    refunds, events = await records(factory)
    assert len(refunds) == len(events) == 1 and refunds[0].amount_minor == 500


@pytest.mark.parametrize("winner", ["capture", "cancel"])
async def test_real_lock_capture_cancellation_race_has_one_compensation_without_deadlock(
    database_session_factory, monkeypatch, winner
):
    factory = database_session_factory
    user, result = await booking(factory)
    entered, release = asyncio.Event(), asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        if winner == "capture":
            original = service.append_outbox_event

            async def pause(*args, **kwargs):
                value = await original(*args, **kwargs)
                if kwargs["event_type"] == "CollectionRequestAccepted":
                    entered.set()
                    await release.wait()
                return value

            monkeypatch.setattr(service, "append_outbox_event", pause)
            first = asyncio.create_task(capture(factory, provider, attempt))
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(cancel(factory, result.request, label="cancel-waiter"))
            try:
                await blocked(factory, "cancel-waiter")
            finally:
                release.set()
        else:
            original = cancellation.ensure_cancellation_refund

            async def pause(*args, **kwargs):
                value = await original(*args, **kwargs)
                entered.set()
                await release.wait()
                return value

            monkeypatch.setattr(cancellation, "ensure_cancellation_refund", pause)
            first = asyncio.create_task(cancel(factory, result.request))
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(capture(factory, provider, attempt))
            # The event reaches Payment first and waits there, holding no Attempt.
            try:
                await blocked(factory)
                assert not second.done()
            finally:
                release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 10)
        await capture(factory, provider, attempt)
        await capture(factory, provider, attempt, "reordered-duplicate")
    refunds, events = await records(factory)
    assert len(refunds) == len(events) == 1
    async with factory() as session:
        request = await session.get(CollectionRequest, result.request.request_id)
        payment = await session.get(Payment, result.payment.payment_id)
        assert request.status == "CANCELLED" and payment.status == "SUCCEEDED"
        assert payment.successful_attempt_id == attempt.payment_attempt_id
        count = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "CollectionRequestAccepted")
        )
        assert count == int(winner == "capture")


@pytest.mark.parametrize("winner", ["freeze", "cancel"])
async def test_real_planning_cancellation_race_revalidates_after_wait(
    database_session_factory, monkeypatch, winner
):
    factory = database_session_factory
    user, result = await booking(factory)
    entered, release = asyncio.Event(), asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        await capture(factory, provider, await initiate(factory, user, result, provider))
    work = planning.PlanningWorkUnit(
        result.request.cell_id, result.request.slot_start, result.request.slot_end
    )

    async def freeze():
        return await planning.freeze_planning_batch(
            factory,
            work,
            lead_time_minutes=30,
            max_attempts=3,
            compaction_distance_m=500,
            max_group_requests=4,
            now=result.request.slot_start,
        )

    if winner == "freeze":
        original = planning.acquire_work_unit_advisory_lock

        async def pause(*args, **kwargs):
            await original(*args, **kwargs)
            entered.set()
            await release.wait()

        monkeypatch.setattr(planning, "acquire_work_unit_advisory_lock", pause)
        first = asyncio.create_task(freeze())
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(cancel(factory, result.request, label="freeze-waiter"))
        try:
            await blocked(factory, "freeze-waiter")
        finally:
            release.set()
        await first
        with pytest.raises(CancellationConflictError) as error:
            await second
        assert error.value.code == "PLANNING_STARTED"
    else:
        original = cancellation.ensure_cancellation_refund

        async def pause(*args, **kwargs):
            value = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return value

        monkeypatch.setattr(cancellation, "ensure_cancellation_refund", pause)
        first = asyncio.create_task(cancel(factory, result.request))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(freeze())
        try:
            await blocked(factory)
            assert not second.done()
        finally:
            release.set()
        await asyncio.wait_for(first, 10)
        assert (await asyncio.wait_for(second, 10)).transitioned_request_count == 0
    refunds, events = await records(factory)
    assert len(refunds) == len(events) == int(winner == "cancel")


@pytest.mark.parametrize("balance", [0, 100, 500])
async def test_prior_refund_full_balance_reuses_obligation_partial_balance_refuses(
    database_session_factory, balance
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        await capture(factory, provider, attempt)
    if balance:
        async with factory() as session, session.begin():
            await create_refund(
                session,
                payment_id=result.payment.payment_id,
                payment_attempt_id=attempt.payment_attempt_id,
                amount_minor=balance,
                reason_code="OPERATIONS_ADJUSTMENT",
                idempotency_key="existing",
                idempotency_expires_at=utc_now() + timedelta(days=1),
            )
    if balance == 100:
        with pytest.raises(CancellationConflictError):
            await cancel(factory, result.request)
        async with factory() as session:
            assert (
                await session.get(CollectionRequest, result.request.request_id)
            ).status == "ACCEPTED"
    else:
        assert (await cancel(factory, result.request)).status == "CANCELLED"
    refunds, events = await records(factory)
    assert len(refunds) == len(events) == 1
    assert refunds[0].amount_minor == (100 if balance == 100 else 500)


@pytest.mark.parametrize("failure", ["refund_outbox", "cancellation_completion"])
async def test_cancellation_crash_before_commit_rolls_back_all_then_retry_converges(
    database_session_factory, monkeypatch, failure
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        await capture(factory, provider, await initiate(factory, user, result, provider))
    from tirodhan.modules.payments import refunds as refunds_module

    target = refunds_module if failure == "refund_outbox" else cancellation
    name = "append_outbox_event" if failure == "refund_outbox" else "complete_idempotency_record"
    original = getattr(target, name)

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated precommit crash")

    with monkeypatch.context() as patch:
        patch.setattr(target, name, crash)
        with pytest.raises(RuntimeError, match="precommit"):
            await cancel(factory, result.request)
    assert await records(factory) == ([], [])
    async with factory() as session:
        assert (
            await session.get(CollectionRequest, result.request.request_id)
        ).status == "ACCEPTED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(
                    IdempotencyRecord.scope
                    == f"collection-request.cancel:{result.request.request_id}"
                )
            )
            == 0
        )
    await cancel(factory, result.request)
    await cancel(factory, result.request)
    refunds, events = await records(factory)
    assert len(refunds) == len(events) == 1


@pytest.mark.parametrize(
    "state, expected",
    [
        ("PENDING", "PENDING"),
        ("CREATED", "PROCESSING"),
        ("INITIATION_UNCERTAIN", "CONFIRMING"),
        ("FAILED", "FAILED"),
        ("EXPIRED", "EXPIRED"),
        ("RECONCILIATION", "CONFIRMING"),
    ],
)
async def test_payment_read_lifecycle_and_retry(database_session_factory, state, expected):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
    async with factory() as session, session.begin():
        if state == "EXPIRED":
            row = await session.get(CollectionRequest, result.request.request_id)
            row.created_at = utc_now() - timedelta(hours=2)
            row.payment_expires_at = utc_now() - timedelta(seconds=1)
        elif state == "RECONCILIATION":
            session.add(
                PaymentProviderEvent(
                    provider="RAZORPAY",
                    external_event_id="review",
                    event_type="payment.captured",
                    payment_attempt_id=attempt.payment_attempt_id,
                    processing_status="RECONCILIATION_REQUIRED",
                )
            )
        else:
            await session.execute(
                update(PaymentAttempt)
                .where(PaymentAttempt.payment_attempt_id == attempt.payment_attempt_id)
                .values(status=state)
            )
    app = app_for(factory, user.user_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        response = await api.get(
            f"/v1/customer/collection-requests/{result.request.request_id}/payment"
        )
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["status"] == expected and value["retry_allowed"] == (state == "FAILED")
        assert value["current_attempt"]["payment_attempt_id"] == str(attempt.payment_attempt_id)


@pytest.mark.parametrize(
    "state, expected",
    [
        ("PENDING", "INITIATED"),
        ("PROCESSING", "PROCESSING"),
        ("SUBMITTED", "PROCESSING"),
        ("INITIATION_UNCERTAIN", "CONFIRMING"),
        ("SUCCEEDED", "COMPLETED"),
        ("FAILED", "FAILED"),
    ],
)
async def test_refund_read_real_lifecycle(database_session_factory, state, expected):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        await capture(factory, provider, await initiate(factory, user, result, provider))
    await cancel(factory, result.request)
    async with factory() as session, session.begin():
        refund = await session.scalar(select(Refund))
        refund.status = state
        refund.completed_at = utc_now() if state in {"SUCCEEDED", "FAILED"} else None
    app = app_for(factory, user.user_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        response = await api.get(
            f"/v1/customer/collection-requests/{result.request.request_id}/refunds"
        )
        assert response.status_code == 200
        value = response.json()["refunds"][0]
        assert value["status"] == expected and value["reason"] == "CUSTOMER_CANCELLATION"
        assert (value["completed_at"] is not None) == (state == "SUCCEEDED")


@pytest.mark.parametrize("suffix", ["payment", "refunds"])
async def test_financial_reads_live_auth_ownership_revocation_and_safe_errors(
    database_session_factory, suffix
):
    factory = database_session_factory
    user, result = await booking(factory)
    app = app_for(factory, None)
    token, session_id = await _token_for(factory, user.user_id, roles=("CUSTOMER",))
    foreign, _ = await booking(factory)
    other_token, _ = await _token_for(factory, foreign.user_id, roles=("CUSTOMER",))
    rider_token, _ = await _token_for(factory, foreign.user_id, roles=("RIDER",))
    # The helper adds roles; retire CUSTOMER to exercise forbidden principal.
    async with factory() as session, session.begin():
        await session.execute(
            update(UserRole)
            .where(UserRole.user_id == foreign.user_id, UserRole.role_code == "CUSTOMER")
            .values(revoked_at=utc_now())
        )
    path = f"/v1/customer/collection-requests/{result.request.request_id}/{suffix}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        assert (await api.get(path)).status_code == 401
        assert (
            await api.get(path, headers={"Authorization": f"Bearer {rider_token}"})
        ).status_code == 403
        # Restore CUSTOMER for the foreign-owner check.
        async with factory() as session, session.begin():
            await session.execute(
                update(UserRole)
                .where(UserRole.user_id == foreign.user_id, UserRole.role_code == "CUSTOMER")
                .values(revoked_at=None)
            )
        assert (
            await api.get(path, headers={"Authorization": f"Bearer {other_token}"})
        ).status_code == 404
        headers = {"Authorization": f"Bearer {token}"}
        assert (await api.get(path, headers=headers)).status_code == 200
        assert (
            await api.get(path.replace(str(result.request.request_id), "invalid"), headers=headers)
        ).status_code == 422
        async with factory() as session, session.begin():
            (await session.get(RefreshSession, session_id)).revoked_at = utc_now()
        assert (await api.get(path, headers=headers)).status_code == 401


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "amount",
        "currency",
        "canonical",
        "refund_currency",
        "refund_time",
        "operations_reason",
    ],
)
async def test_financial_corruption_or_unmapped_reason_is_sanitized(database_session_factory, case):
    factory = database_session_factory
    user, result = await booking(factory)
    if case != "missing":
        async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
            provider = RazorpayProvider(settings(), client=remote)
            await capture(factory, provider, await initiate(factory, user, result, provider))
        await cancel(factory, result.request)
    async with factory() as session, session.begin():
        payment = await session.get(Payment, result.payment.payment_id)
        if case == "missing":
            await session.delete(payment)
        elif case == "amount":
            payment.amount_minor = 501
        elif case == "currency":
            payment.currency = "USD"
        elif case == "canonical":
            payment.successful_attempt_id = None
        else:
            refund = await session.scalar(select(Refund))
            if case == "refund_currency":
                refund.currency = "USD"
            elif case == "refund_time":
                refund.status = "SUCCEEDED"
                refund.completed_at = None
            else:
                refund.reason_code = "OPERATIONS_ADJUSTMENT"
    app = app_for(factory, user.user_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        response = await api.get(
            f"/v1/customer/collection-requests/{result.request.request_id}/refunds"
        )
        assert response.status_code == 503 and response.json() == {
            "error": {"code": "NOT_ELIGIBLE"}
        }
        assert "pay_success" not in response.text


@pytest.mark.parametrize("mode", ["normal", "lost_response", "overlap"])
async def test_cancellation_origin_refund_worker_retains_one_native_effect(
    database_session_factory, database_engine, mode
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        await capture(factory, provider, await initiate(factory, user, result, provider))
    await cancel(factory, result.request)
    refund = (await records(factory))[0][0]
    refunds = Refunds()
    both = asyncio.Event()
    calls = 0
    delivery = str(new_uuid7())

    async def transport(request):
        nonlocal calls
        if mode != "overlap":
            assert database_engine.pool.checkedout() == 0
        calls += 1
        response = refunds(request)
        if mode == "lost_response" and calls == 1:
            raise httpx.ReadTimeout("ambiguous", request=request)
        if mode == "overlap":
            if calls == 2:
                # Both calls are now in HTTP, before either can persist its result.
                assert database_engine.pool.checkedout() == 0
                both.set()
            await asyncio.wait_for(both.wait(), 5)
        data = response.json()
        data["payment_id"] = "pay_success"
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        if mode == "lost_response":
            with pytest.raises(RefundConflictError):
                await message(factory, provider, refund, delivery)
        if mode == "overlap":
            await asyncio.wait_for(
                asyncio.gather(
                    message(factory, provider, refund, str(new_uuid7())),
                    message(factory, provider, refund, str(new_uuid7())),
                ),
                10,
            )
        else:
            await message(factory, provider, refund, delivery)
        await message(factory, provider, refund, delivery)
    assert len(refunds.effects) == 1
    assert all(call == refunds.calls[0] for call in refunds.calls)
    assert refunds.calls[0][0] == f"rf_{refund.refund_id.hex}"
    async with factory() as session:
        stored = await session.get(Refund, refund.refund_id)
        assert stored.status == "SUCCEEDED" and stored.completed_at is not None


@pytest.mark.parametrize("late", ["expired", "frozen", "uncertain"])
async def test_cancelled_late_capture_compensates_even_after_expiry_or_freeze(
    database_session_factory, late
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        if late == "uncertain":
            async with factory() as session, session.begin():
                (
                    await session.get(PaymentAttempt, attempt.payment_attempt_id)
                ).status = "INITIATION_UNCERTAIN"
        await cancel(factory, result.request)
        async with factory() as session, session.begin():
            if late == "expired":
                row = await session.get(CollectionRequest, result.request.request_id)
                row.created_at = utc_now() - timedelta(hours=2)
                row.payment_expires_at = utc_now() - timedelta(hours=1)
            elif late == "frozen":
                session.add(
                    PlanningBatch(
                        cell_id=result.request.cell_id,
                        slot_start=result.request.slot_start,
                        slot_end=result.request.slot_end,
                        status="READY",
                        max_attempts_snapshot=3,
                    )
                )
        event = await capture(factory, provider, attempt)
        assert event.failure_code == "CANCELLED_CAPTURE_COMPENSATED"
        await capture(factory, provider, attempt)
    refunds, events = await records(factory)
    assert len(refunds) == len(events) == 1 and refunds[0].amount_minor == 500
    async with factory() as session:
        assert (
            await session.get(CollectionRequest, result.request.request_id)
        ).status == "CANCELLED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "CollectionRequestAccepted")
            )
            == 0
        )


async def test_additional_distinct_charge_preserves_reconciliation_facts_and_never_reopens(
    database_session_factory,
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        await capture(factory, provider, attempt)
        await cancel(factory, result.request)
        body = payment_body(attempt, payment_id="pay_additional")
        event = await provider.authenticate_webhook(
            raw_body=body, headers=signed(body, "additional")
        )
        record = await service.process_authenticated_payment_event(
            factory,
            event,
            payload_hash=hashlib.sha256(body).digest(),
            planning_lead_time_minutes=30,
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )
    assert record.processing_status == "RECONCILIATION_REQUIRED"
    assert record.provider_payment_id == "pay_additional"
    assert record.amount_minor == 500 and record.currency == "INR"
    refunds, _ = await records(factory)
    assert len(refunds) == 1
    async with factory() as session:
        assert (
            await session.get(CollectionRequest, result.request.request_id)
        ).status == "CANCELLED"
        assert (
            await session.get(PaymentAttempt, attempt.payment_attempt_id)
        ).provider_payment_id == "pay_success"


async def test_missing_compensation_config_rolls_back_capture_until_retry(database_session_factory):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        await cancel(factory, result.request)
        body = payment_body(attempt)
        event = await provider.authenticate_webhook(raw_body=body, headers=signed(body, "capture"))
        with pytest.raises(service.PaymentNotEligibleError):
            await service.process_authenticated_payment_event(
                factory,
                event,
                payload_hash=hashlib.sha256(body).digest(),
                planning_lead_time_minutes=30,
            )
        async with factory() as session:
            assert (await session.get(Payment, result.payment.payment_id)).status == "CANCELLED"
            assert await session.scalar(select(func.count()).select_from(PaymentProviderEvent)) == 0
        await capture(factory, provider, attempt)
    assert len((await records(factory))[0]) == 1


async def test_compensation_failure_rolls_back_late_capture_and_retries_once(
    database_session_factory, monkeypatch
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        await cancel(factory, result.request)
        from tirodhan.modules.payments import refunds as refund_service

        original = refund_service.append_outbox_event

        async def fail(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("compensation rollback")

        monkeypatch.setattr(refund_service, "append_outbox_event", fail)
        with pytest.raises(RuntimeError, match="compensation rollback"):
            await capture(factory, provider, attempt)
        async with factory() as session:
            assert (await session.get(Payment, result.payment.payment_id)).status == "CANCELLED"
            assert (
                await session.get(PaymentAttempt, attempt.payment_attempt_id)
            ).status == "PENDING"
            assert await session.scalar(select(func.count()).select_from(PaymentProviderEvent)) == 0
        assert await records(factory) == ([], [])
        monkeypatch.setattr(refund_service, "append_outbox_event", original)
        await capture(factory, provider, attempt)
        await capture(factory, provider, attempt, "duplicate-capture")
    assert len((await records(factory))[0]) == len((await records(factory))[1]) == 1


async def test_different_attempt_keys_serialize_and_only_failed_attempt_can_retry(
    database_session_factory,
):
    factory = database_session_factory
    user, result = await booking(factory)
    orders = Orders()
    async with httpx.AsyncClient(transport=httpx.MockTransport(orders)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        results = await asyncio.wait_for(
            asyncio.gather(
                initiate(factory, user, result, provider, "first"),
                initiate(factory, user, result, provider, "second"),
                return_exceptions=True,
            ),
            10,
        )
        attempts = [r for r in results if isinstance(r, PaymentAttempt)]
        assert len(attempts) == 1
        assert sum(isinstance(r, service.PaymentNotEligibleError) for r in results) == 1
        async with factory() as session, session.begin():
            (await session.get(PaymentAttempt, attempts[0].payment_attempt_id)).status = "FAILED"
        retry = await initiate(factory, user, result, provider, "retry")
        assert retry.payment_attempt_id != attempts[0].payment_attempt_id
        replay = await initiate(factory, user, result, provider, "retry")
        assert replay.payment_attempt_id == retry.payment_attempt_id
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 2


async def test_historical_refunds_are_ordered_and_financial_reads_are_bounded_and_read_only(
    database_session_factory, database_engine, address_protector
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        await capture(factory, provider, attempt)
    identities = []
    for index, (amount, status) in enumerate(
        [(200, "FAILED"), (200, "SUCCEEDED"), (300, "PENDING")]
    ):
        async with factory() as session, session.begin():
            refund = await create_refund(
                session,
                payment_id=result.payment.payment_id,
                payment_attempt_id=attempt.payment_attempt_id,
                amount_minor=amount,
                reason_code="CUSTOMER_CANCELLATION",
                idempotency_key=f"historical-{index}",
                idempotency_expires_at=utc_now() + timedelta(days=1),
            )
            refund.status = status
            refund.completed_at = utc_now() if status == "SUCCEEDED" else None
            identities.append(str(refund.refund_id))
    await cancel(factory, result.request)
    before = await financial_snapshot(factory)
    refunds_before, _ = await records(factory)
    before_refunds = [
        tuple(getattr(r, c.key) for c in Refund.__table__.columns) for r in refunds_before
    ]
    statements = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    app = app_for(factory, user.user_id, address_protector)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        sql_event.listen(database_engine.sync_engine, "before_cursor_execute", observe)
        try:
            base = f"/v1/customer/collection-requests/{result.request.request_id}"
            for _ in range(2):
                for path, maximum in [("/payment", 6), ("/refunds", 5)]:
                    statements.clear()
                    response = await api.get(base + path)
                    assert response.status_code == 200
                    assert response.headers["cache-control"] == "private, no-store"
                    assert (
                        sum(s.lstrip().upper().startswith("SELECT") for s in statements) <= maximum
                    )
                    assert not any(
                        s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                        for s in statements
                    )
                    if path == "/refunds":
                        assert [r["refund_id"] for r in response.json()["refunds"]] == identities
                        assert [r["status"] for r in response.json()["refunds"]] == [
                            "FAILED",
                            "COMPLETED",
                            "INITIATED",
                        ]
        finally:
            sql_event.remove(database_engine.sync_engine, "before_cursor_execute", observe)
    assert await financial_snapshot(factory) == before
    refunds_after, _ = await records(factory)
    assert [
        tuple(getattr(r, c.key) for c in Refund.__table__.columns) for r in refunds_after
    ] == before_refunds


@pytest.mark.parametrize("case", ["cutoff", "freeze", "partial", "configuration"])
async def test_cancellation_api_authoritative_refusal_and_safe_error_envelope(
    database_session_factory, case
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        await capture(factory, provider, attempt)
    app = app_for(factory, user.user_id)
    if case in {"cutoff", "configuration"}:
        app.state.settings = app.state.settings.model_copy(
            update={"planning_lead_time_minutes": 1000000 if case == "cutoff" else None}
        )
    elif case == "freeze":
        async with factory() as session, session.begin():
            session.add(
                PlanningBatch(
                    cell_id=result.request.cell_id,
                    slot_start=result.request.slot_start,
                    slot_end=result.request.slot_end,
                    status="READY",
                    max_attempts_snapshot=3,
                )
            )
    else:
        async with factory() as session, session.begin():
            await create_refund(
                session,
                payment_id=result.payment.payment_id,
                payment_attempt_id=attempt.payment_attempt_id,
                amount_minor=100,
                reason_code="CUSTOMER_CANCELLATION",
                idempotency_key="partial",
                idempotency_expires_at=utc_now() + timedelta(days=1),
            )
    before = await financial_snapshot(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        response = await api.post(
            f"/v1/collection-requests/{result.request.request_id}/cancel",
            headers={"Idempotency-Key": "refused"},
        )
        assert response.status_code == (503 if case == "configuration" else 409)
        assert response.json() == {
            "error": {
                "code": {
                    "cutoff": "PLANNING_CUTOFF_REACHED",
                    "freeze": "PLANNING_STARTED",
                    "partial": "NOT_ELIGIBLE",
                    "configuration": "NOT_ELIGIBLE",
                }[case]
            }
        }
    assert await financial_snapshot(factory) == before


async def test_accepted_request_without_captured_payment_is_inconsistent(database_session_factory):
    factory = database_session_factory
    user, result = await booking(factory)
    async with factory() as session, session.begin():
        request = await session.get(CollectionRequest, result.request.request_id)
        request.status = "ACCEPTED"
        request.accepted_at = utc_now()
    with pytest.raises(CancellationConflictError):
        await cancel(factory, result.request)
    app = app_for(factory, user.user_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        for suffix in ("payment", "refunds"):
            response = await api.get(
                f"/v1/customer/collection-requests/{result.request.request_id}/{suffix}"
            )
            assert response.status_code == 503
    assert await records(factory) == ([], [])
