from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from razorpay_helpers import process, settings
from sqlalchemy import event, func, select
from test_cancellation_refunds import create_dummy_request, create_successful_payment

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.payments.models import Refund
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.payments.refund_consumer import (
    CONSUMER_NAME,
    handle_refund_delivery,
    process_refund_message,
)
from tirodhan.modules.payments.refunds import (
    RefundConflictError,
    create_refund,
    execute_refund_provider_call,
)
from tirodhan.modules.reliability.models import InboxMessage

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def intent(factory):
    async with factory() as session, session.begin():
        request = await create_dummy_request(session)
        payment, attempt = await create_successful_payment(session, request)
        attempt.provider = "RAZORPAY"
        attempt.provider_payment_id = "pay_canonical"
        await session.flush()
        return await create_refund(
            session,
            payment_id=payment.payment_id,
            payment_attempt_id=attempt.payment_attempt_id,
            amount_minor=500,
            reason_code="OPERATIONS_ADJUSTMENT",
            idempotency_key="authorized-refund",
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )


class Refunds:
    def __init__(self, state="processed"):
        self.state = state
        self.calls = []
        self.effects = {}

    def __call__(self, request):
        if request.method == "GET":
            # Explicit synthetic account replay-window policy tests: inventory has
            # not yet become visible, forcing identical native-operation recovery.
            return httpx.Response(200, json={"entity": "collection", "count": 0, "items": []})
        body = json.loads(request.content)
        key = request.headers["x-refund-idempotency"]
        self.calls.append((key, request.content))
        if key in self.effects:
            assert self.effects[key]["body"] == body
        else:
            self.effects[key] = {"body": body}
        return httpx.Response(
            200,
            json={
                "entity": "refund",
                "id": "rfnd_canonical",
                "payment_id": "pay_canonical",
                "currency": "INR",
                "status": self.state,
                **body,
            },
        )


def body(refund, *, kind="refund.processed", internal=True, **patch):
    entity = {
        "entity": "refund",
        "id": "rfnd_canonical",
        "payment_id": "pay_canonical",
        "amount": 500,
        "currency": "INR",
        "status": "processed"
        if kind == "refund.processed"
        else "failed"
        if kind == "refund.failed"
        else "pending",
    }
    if internal:
        entity.update(
            receipt=f"rf_{refund.refund_id.hex}",
            notes={"tirodhan_refund_id": str(refund.refund_id)},
        )
    return json.dumps({"event": kind, "payload": {"refund": {"entity": entity | patch}}}).encode()


async def message(factory, provider, refund, message_id):
    await process_refund_message(
        factory,
        message_id=message_id,
        message_type="RefundRequested",
        body=json.dumps({"refund_id": str(refund.refund_id)}).encode(),
        provider=provider,
    )


@pytest.mark.parametrize(
    "provider_state,local_state",
    [("processed", "SUCCEEDED"), ("pending", "SUBMITTED"), ("failed", "FAILED")],
)
async def test_refund_worker_terminal_initiation_duplicate_delivery_and_no_db_connection(
    database_session_factory, database_engine, provider_state, local_state
):
    factory = database_session_factory
    refund = await intent(factory)
    remote = Refunds(provider_state)

    async def transport(request):
        assert database_engine.pool.checkedout() == 0
        async with factory() as session:
            row = await session.get(Refund, refund.refund_id)
            assert (
                row.status == "PROCESSING"
                and row.provider_idempotency_key == f"rf_{refund.refund_id.hex}"
            )
        return remote(request)

    message_id = str(new_uuid7())
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(
            settings(razorpay_refund_replay_window_seconds=600), client=client
        )
        for identity in [message_id, message_id, str(new_uuid7())]:
            await message(factory, provider, refund, identity)
    assert len(remote.calls) == len(remote.effects) == 1
    async with factory() as session:
        row = await session.get(Refund, refund.refund_id)
        assert row.status == local_state and row.provider_refund_id == "rfnd_canonical"
        inbox = list(await session.scalars(select(InboxMessage)))
        assert len(inbox) == 2 and all(row.status == "PROCESSED" for row in inbox)


@pytest.mark.parametrize(
    "crash", ["before_call", "after_remote", "after_remote_before_local_commit"]
)
async def test_refund_crash_redelivery_resumes_identical_key_body_one_external_effect(
    database_session_factory, database_engine, monkeypatch, crash
):
    factory = database_session_factory
    refund = await intent(factory)
    remote = Refunds()
    die = True

    async def transport(request):
        nonlocal die
        response = remote(request)
        if die and crash == "after_remote":
            die = False
            raise asyncio.CancelledError()
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(
            settings(razorpay_refund_replay_window_seconds=600), client=client
        )
        message_id = str(new_uuid7())
        if crash == "before_call":
            async with factory() as session, session.begin():
                row = await session.get(Refund, refund.refund_id)
                row.status = "PROCESSING"
                row.processing_started_at = utc_now()
        elif crash == "after_remote":
            with pytest.raises(asyncio.CancelledError):
                await message(factory, provider, refund, message_id)
        else:

            def fail_local_commit(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("UPDATE refund") and "SUCCEEDED" in parameters:
                    raise RuntimeError("local persistence interruption")

            event.listen(database_engine.sync_engine, "before_cursor_execute", fail_local_commit)
            try:
                with pytest.raises(RuntimeError, match="local persistence"):
                    await message(factory, provider, refund, message_id)
            finally:
                event.remove(
                    database_engine.sync_engine, "before_cursor_execute", fail_local_commit
                )
        async with factory() as session:
            row = await session.get(Refund, refund.refund_id)
            assert row.status == "PROCESSING" and row.provider_refund_id is None
        await message(factory, provider, refund, message_id)
    assert len(remote.effects) == 1
    assert len(remote.calls) == (1 if crash == "before_call" else 2)
    assert all(call == remote.calls[0] for call in remote.calls)
    async with factory() as session:
        row = await session.get(Refund, refund.refund_id)
        assert row.status == "SUCCEEDED" and row.provider_refund_id == "rfnd_canonical"
        assert (await session.get(InboxMessage, (CONSUMER_NAME, message_id))).status == "PROCESSED"


async def test_uncertain_provider_abandons_processing_inbox_then_retry_converges(
    database_session_factory,
):
    factory = database_session_factory
    refund = await intent(factory)
    remote = Refunds()
    uncertain = True

    def transport(request):
        response = remote(request)
        if uncertain:
            raise httpx.ReadTimeout("provider private payload", request=request)
        return response

    class Delivery:
        message_id = str(new_uuid7())
        message_type = "RefundRequested"
        body = json.dumps({"refund_id": str(refund.refund_id)}).encode()
        settled = []

        async def complete(self):
            self.settled.append("complete")

        async def abandon(self):
            self.settled.append("abandon")

        async def dead_letter(self):
            self.settled.append("dead_letter")

    delivery = Delivery()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(
            settings(razorpay_refund_replay_window_seconds=600), client=client
        )
        with pytest.raises(RefundConflictError):
            await handle_refund_delivery(delivery, factory, provider=provider)
        async with factory() as session:
            assert (await session.get(Refund, refund.refund_id)).status == "INITIATION_UNCERTAIN"
            assert (
                await session.get(InboxMessage, (CONSUMER_NAME, delivery.message_id))
            ).status == "PROCESSING"
        uncertain = False
        await handle_refund_delivery(delivery, factory, provider=provider)
    assert delivery.settled == ["abandon", "complete"]
    assert len(remote.effects) == 1 and remote.calls[0] == remote.calls[1]


async def test_overlapping_refund_deliveries_reuse_native_key_and_single_refund(
    database_session_factory,
):
    factory = database_session_factory
    refund = await intent(factory)
    remote = Refunds()
    count = 0
    entered = asyncio.Event()

    async def transport(request):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
        await asyncio.wait_for(entered.wait(), 5)
        return remote(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(
            settings(razorpay_refund_replay_window_seconds=600), client=client
        )
        identity = str(new_uuid7())
        await asyncio.gather(
            message(factory, provider, refund, identity),
            message(factory, provider, refund, identity),
        )
    assert (
        len(remote.calls) == 2 and len(remote.effects) == 1 and remote.calls[0] == remote.calls[1]
    )
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Refund)) == 1
        assert (await session.get(Refund, refund.refund_id)).status == "SUCCEEDED"
        assert (await session.get(InboxMessage, (CONSUMER_NAME, identity))).status == "PROCESSED"


async def test_webhook_terminal_truth_wins_stale_worker_pending_response(database_session_factory):
    factory = database_session_factory
    refund = await intent(factory)
    remote = Refunds("pending")
    entered, release = asyncio.Event(), asyncio.Event()

    async def transport(request):
        response = remote(request)
        entered.set()
        await release.wait()
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(
            settings(razorpay_refund_replay_window_seconds=600), client=client
        )
        worker = asyncio.create_task(
            execute_refund_provider_call(factory, refund.refund_id, provider)
        )
        await asyncio.wait_for(entered.wait(), 5)
        result = await process(factory, provider, body(refund), "evt_refund_wins")
        assert result.processing_status == "PROCESSED"
        async with factory() as session:
            completed = (await session.get(Refund, refund.refund_id)).completed_at
        release.set()
        await worker
    async with factory() as session:
        row = await session.get(Refund, refund.refund_id)
        assert (
            row.status == "SUCCEEDED"
            and row.completed_at == completed
            and row.provider_refund_id == "rfnd_canonical"
        )


async def test_worker_conflicting_refund_id_does_not_overwrite(database_session_factory):
    factory = database_session_factory
    refund = await intent(factory)
    async with factory() as session, session.begin():
        row = await session.get(Refund, refund.refund_id)
        row.provider_refund_id = "rfnd_established"
    async with httpx.AsyncClient(transport=httpx.MockTransport(Refunds())) as client:
        await execute_refund_provider_call(
            factory,
            refund.refund_id,
            RazorpayProvider(settings(razorpay_refund_replay_window_seconds=600), client=client),
        )
    async with factory() as session:
        row = await session.get(Refund, refund.refund_id)
        assert row.provider_refund_id == "rfnd_established" and row.status == "INITIATION_UNCERTAIN"


@pytest.mark.parametrize("correlation", ["provider_id", "receipt", "notes"])
async def test_refund_webhook_correlation_after_lost_response_and_duplicate(
    correlation, database_session_factory
):
    factory = database_session_factory
    refund = await intent(factory)
    patches = {}
    if correlation == "provider_id":
        async with factory() as session, session.begin():
            (await session.get(Refund, refund.refund_id)).provider_refund_id = "rfnd_canonical"
    elif correlation == "receipt":
        patches["notes"] = {}
    else:
        patches["receipt"] = None
    raw = body(refund, internal=correlation != "provider_id", **patches)
    async with httpx.AsyncClient() as client:
        provider = RazorpayProvider(
            settings(razorpay_refund_replay_window_seconds=600), client=client
        )
        a, b = await asyncio.gather(
            process(factory, provider, raw), process(factory, provider, raw)
        )
    assert (
        a.payment_provider_event_id == b.payment_provider_event_id
        and a.processing_status == "PROCESSED"
    )
    async with factory() as session:
        row = await session.get(Refund, refund.refund_id)
        assert row.status == "SUCCEEDED" and row.provider_refund_id == "rfnd_canonical"


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"amount": 1}, "REFUND_AMOUNT_MISMATCH"),
        ({"currency": "USD"}, "REFUND_CURRENCY_MISMATCH"),
        ({"payment_id": "pay_other"}, "REFUND_PAYMENT_MISMATCH"),
        ({"receipt": "rf_" + new_uuid7().hex}, "REFUND_IDENTITY_CONFLICT"),
    ],
)
async def test_refund_webhook_contradictory_facts_reconcile_without_mutation(
    database_session_factory, patch, code
):
    factory = database_session_factory
    refund = await intent(factory)
    async with httpx.AsyncClient() as client:
        record = await process(
            factory,
            RazorpayProvider(settings(razorpay_refund_replay_window_seconds=600), client=client),
            body(refund, **patch),
        )
        assert record.processing_status == "RECONCILIATION_REQUIRED" and record.failure_code == code
    async with factory() as session:
        row = await session.get(Refund, refund.refund_id)
        assert row.status == "PENDING" and row.provider_refund_id is None


async def test_refund_created_nonterminal_failed_then_processed_is_conflict(
    database_session_factory,
):
    factory = database_session_factory
    refund = await intent(factory)
    async with httpx.AsyncClient() as client:
        provider = RazorpayProvider(
            settings(razorpay_refund_replay_window_seconds=600), client=client
        )
        created = await process(
            factory, provider, body(refund, kind="refund.created"), "evt_created"
        )
        assert created.processing_status == "PROCESSED"
        async with factory() as session:
            row = await session.get(Refund, refund.refund_id)
            assert row.status == "SUBMITTED" and row.completed_at is None
        failed = await process(factory, provider, body(refund, kind="refund.failed"), "evt_failed")
        assert failed.processing_status == "PROCESSED"
        late = await process(factory, provider, body(refund), "evt_late")
        assert late.processing_status == "RECONCILIATION_REQUIRED"
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "FAILED"


@pytest.mark.parametrize(
    "raw",
    [
        b"invalid",
        b'{"refund_id":"invalid"}',
        b'{"refund_id":"00000000-0000-0000-0000-000000000000","address":"not allowed"}',
    ],
)
async def test_invalid_refund_delivery_dead_letters_without_inbox(database_session_factory, raw):
    class Delivery:
        message_id = str(new_uuid7())
        message_type = "RefundRequested"
        body = raw
        settled = []

        async def complete(self):
            self.settled.append("complete")

        async def abandon(self):
            self.settled.append("abandon")

        async def dead_letter(self):
            self.settled.append("dead_letter")

    delivery = Delivery()
    async with httpx.AsyncClient() as client:
        await handle_refund_delivery(
            delivery,
            database_session_factory,
            provider=RazorpayProvider(
                settings(razorpay_refund_replay_window_seconds=600), client=client
            ),
        )
    assert delivery.settled == ["dead_letter"]
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 0


async def test_refund_merchant_failure_is_retryable_not_financial_failure(database_session_factory):
    from tirodhan.modules.payments.ports import PaymentProviderNotConfiguredError

    factory = database_session_factory
    refund = await intent(factory)
    invalid = True
    remote = Refunds()

    def transport(request):
        if invalid:
            return httpx.Response(401, json={"error": {}})
        return remote(request)

    identity = str(new_uuid7())
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(
            settings(razorpay_refund_replay_window_seconds=600), client=client
        )
        with pytest.raises(PaymentProviderNotConfiguredError):
            await message(factory, provider, refund, identity)
        async with factory() as session:
            assert (await session.get(Refund, refund.refund_id)).status == "PROCESSING"
            assert (
                await session.get(InboxMessage, (CONSUMER_NAME, identity))
            ).status == "PROCESSING"
        invalid = False
        await message(factory, provider, refund, identity)
    assert len(remote.effects) == 1
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "SUCCEEDED"


async def test_refund_success_is_not_downgraded_by_stale_created_or_failed_event(
    database_session_factory,
):
    factory = database_session_factory
    refund = await intent(factory)
    async with httpx.AsyncClient() as client:
        provider = RazorpayProvider(
            settings(razorpay_refund_replay_window_seconds=600), client=client
        )
        await process(factory, provider, body(refund), "evt_done")
        async with factory() as session:
            completed = (await session.get(Refund, refund.refund_id)).completed_at
        created = await process(
            factory, provider, body(refund, kind="refund.created"), "evt_stale_created"
        )
        failed = await process(
            factory, provider, body(refund, kind="refund.failed"), "evt_stale_failed"
        )
        assert created.processing_status == "PROCESSED"
        assert failed.processing_status == "RECONCILIATION_REQUIRED"
    async with factory() as session:
        row = await session.get(Refund, refund.refund_id)
        assert (
            row.status == "SUCCEEDED"
            and row.completed_at == completed
            and row.provider_refund_id == "rfnd_canonical"
        )


async def test_known_external_refund_with_contradictory_receipt_cannot_mutate(
    database_session_factory,
):
    factory = database_session_factory
    refund = await intent(factory)
    async with factory() as session, session.begin():
        (await session.get(Refund, refund.refund_id)).provider_refund_id = "rfnd_canonical"
    async with httpx.AsyncClient() as client:
        record = await process(
            factory,
            RazorpayProvider(settings(razorpay_refund_replay_window_seconds=600), client=client),
            body(refund, internal=False, receipt="unrelated_receipt"),
        )
        assert record.processing_status == "RECONCILIATION_REQUIRED"
        assert record.failure_code == "REFUND_IDENTITY_CONFLICT"
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "PENDING"


async def test_refund_unknown_identity_cannot_hijack_known_external_refund(
    database_session_factory,
):
    factory = database_session_factory
    refund = await intent(factory)
    async with factory() as session, session.begin():
        (await session.get(Refund, refund.refund_id)).provider_refund_id = "rfnd_canonical"
    other = new_uuid7()
    async with httpx.AsyncClient() as client:
        record = await process(
            factory,
            RazorpayProvider(settings(razorpay_refund_replay_window_seconds=600), client=client),
            body(refund, receipt=f"rf_{other.hex}", notes={"tirodhan_refund_id": str(other)}),
        )
        assert (
            record.processing_status == "RECONCILIATION_REQUIRED"
            and record.failure_code == "REFUND_IDENTITY_CONFLICT"
        )
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "PENDING"
