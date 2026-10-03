from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select
from test_razorpay_payments import ProviderHarness, attempt_for, booking

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.payments.models import Payment, Refund
from tirodhan.modules.payments.ports import PaymentProviderNotConfiguredError
from tirodhan.modules.payments.refund_consumer import handle_refund_delivery, process_refund_message
from tirodhan.modules.payments.refunds import create_refund
from tirodhan.modules.reliability.models import InboxMessage
from tirodhan.modules.reliability.primitives import claim_inbox_message

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def refund_harness():
    harness = ProviderHarness()
    original = harness.send

    async def send(request):
        if not request.url.path.endswith("/refund"):
            return await original(request)
        harness.calls.append(request)
        if harness.on_send:
            await harness.on_send(request)
        if harness.mode == "timeout":
            raise httpx.ReadTimeout("not persisted", request=request)
        if harness.mode == "cancel":
            raise asyncio.CancelledError
        if harness.mode == "merchant":
            return httpx.Response(401, json={"error": {"description": "not persisted"}})
        if harness.mode == "failed":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "BAD_REQUEST_ERROR",
                        "reason": "input_validation_failed",
                        "field": "amount",
                    }
                },
            )
        if harness.mode == "malformed":
            return httpx.Response(200, json={"untrusted": "not persisted"})
        data = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "entity": "refund",
                "id": "rfnd_" + data["notes"]["tirodhan_refund_id"].replace("-", ""),
                "payment_id": request.url.path.split("/")[-2],
                "amount": data["amount"],
                "currency": "INR",
                "status": "pending" if harness.mode == "pending" else "processed",
            },
        )

    await harness.client.aclose()
    harness.client = httpx.AsyncClient(transport=httpx.MockTransport(send))
    from tirodhan.modules.payments.razorpay import RazorpayProvider

    harness.provider = RazorpayProvider(harness.settings, client=harness.client)
    try:
        yield harness
    finally:
        await harness.client.aclose()


async def refund_intent(factory, harness):
    user, result = await booking(factory)
    attempt = await attempt_for(factory, harness, user, result)
    await harness.process(factory, harness.body(attempt))
    async with factory() as session, session.begin():
        refund = await create_refund(
            session,
            payment_id=result.payment.payment_id,
            payment_attempt_id=attempt.payment_attempt_id,
            amount_minor=100,
            reason_code="OPERATIONS_ADJUSTMENT",
            idempotency_key="refund",
            idempotency_expires_at=utc_now() + timedelta(hours=1),
        )
    harness.calls.clear()
    return refund, attempt


def envelope(refund_id, message_id=None):
    return {
        "message_id": str(message_id or new_uuid7()),
        "message_type": "RefundRequested",
        "body": json.dumps({"refund_id": str(refund_id)}).encode(),
    }


class Delivery:
    def __init__(self, **fields):
        self.__dict__.update(fields)
        self.settled = None

    async def complete(self):
        self.settled = "complete"

    async def abandon(self):
        self.settled = "abandon"

    async def dead_letter(self):
        self.settled = "dead_letter"


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("ready", "SUCCEEDED"),
        ("pending", "SUBMITTED"),
        ("failed", "FAILED"),
        ("timeout", "INITIATION_UNCERTAIN"),
        ("malformed", "INITIATION_UNCERTAIN"),
    ],
)
async def test_refund_delivery_result_durable_duplicates_and_no_second_post(
    database_session_factory, database_engine, refund_harness, mode, expected
):
    factory, harness = database_session_factory, refund_harness
    refund, attempt = await refund_intent(factory, harness)
    harness.mode = mode

    async def outside_transaction(request):
        assert database_engine.pool.checkedout() == 0
        assert request.url.path == f"/v1/payments/pay_{attempt.payment_attempt_id.hex}/refund"
        assert json.loads(request.content)["amount"] == refund.amount_minor
        assert (
            request.headers["x-refund-idempotency"]
            == hashlib.sha256(refund.provider_idempotency_key.encode()).hexdigest()
        )
        async with factory() as session:
            assert (await session.get(Refund, refund.refund_id)).status == "PROCESSING"

    harness.on_send = outside_transaction
    delivery = Delivery(**envelope(refund.refund_id))
    await handle_refund_delivery(delivery, factory, provider=harness.provider)
    await handle_refund_delivery(delivery, factory, provider=harness.provider)
    other = Delivery(**envelope(refund.refund_id))
    await handle_refund_delivery(other, factory, provider=harness.provider)
    assert delivery.settled == other.settled == "complete"
    assert len(harness.calls) == 1
    async with factory() as session:
        durable = await session.get(Refund, refund.refund_id)
        assert durable.status == expected
        assert durable.provider_refund_id == (
            "rfnd_" + refund.refund_id.hex if mode in {"ready", "pending"} else None
        )
        assert {row.status for row in await session.scalars(select(InboxMessage))} == {"PROCESSED"}


async def test_pre_provider_crash_resumes_pending_refund(database_session_factory, refund_harness):
    factory, harness = database_session_factory, refund_harness
    refund, _ = await refund_intent(factory, harness)
    message = envelope(refund.refund_id)
    async with factory() as session, session.begin():
        await claim_inbox_message(
            session,
            consumer_name="refund-execution",
            message_id=message["message_id"],
            message_type=message["message_type"],
            business_key=str(refund.refund_id),
        )
    await process_refund_message(factory, **message, provider=harness.provider)
    assert len(harness.calls) == 1
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "SUCCEEDED"


async def test_interrupted_processing_commit_redelivery_reconciles_without_second_call(
    database_session_factory, refund_harness
):
    factory, harness = database_session_factory, refund_harness
    refund, _ = await refund_intent(factory, harness)
    message = envelope(refund.refund_id)
    harness.mode = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await process_refund_message(factory, **message, provider=harness.provider)
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "PROCESSING"
        assert (
            await session.get(InboxMessage, ("refund-execution", message["message_id"]))
        ).status == "PROCESSING"
    harness.mode = "ready"
    await process_refund_message(factory, **message, provider=harness.provider)
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "INITIATION_UNCERTAIN"
        assert (
            await session.get(InboxMessage, ("refund-execution", message["message_id"]))
        ).status == "PROCESSED"
    assert len(harness.calls) == 1


async def test_known_provider_result_local_rollback_does_not_repeat_money_movement(
    database_session_factory, database_engine, refund_harness
):
    factory, harness = database_session_factory, refund_harness
    refund, _ = await refund_intent(factory, harness)
    message = envelope(refund.refund_id)

    def fail_result(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE refund") and "SUCCEEDED" in parameters:
            raise RuntimeError("simulated local persistence failure")

    sqlalchemy_event.listen(database_engine.sync_engine, "before_cursor_execute", fail_result)
    try:
        with pytest.raises(RuntimeError, match="simulated local persistence"):
            await process_refund_message(factory, **message, provider=harness.provider)
    finally:
        sqlalchemy_event.remove(database_engine.sync_engine, "before_cursor_execute", fail_result)
    await process_refund_message(factory, **message, provider=harness.provider)
    assert len(harness.calls) == 1
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "INITIATION_UNCERTAIN"


async def test_concurrent_refund_messages_do_not_duplicate_invocation(
    database_session_factory, refund_harness
):
    factory, harness = database_session_factory, refund_harness
    refund, _ = await refund_intent(factory, harness)
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(request):
        entered.set()
        await release.wait()

    harness.on_send = block
    task = asyncio.create_task(
        process_refund_message(factory, **envelope(refund.refund_id), provider=harness.provider)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await process_refund_message(
            factory, **envelope(refund.refund_id), provider=harness.provider
        )
    finally:
        release.set()
    await asyncio.wait_for(task, 5)
    assert len(harness.calls) == 1
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "SUCCEEDED"


async def test_configuration_failure_is_abandoned_then_durable_uncertainty_settles(
    database_session_factory, refund_harness
):
    factory, harness = database_session_factory, refund_harness
    refund, _ = await refund_intent(factory, harness)
    harness.mode = "merchant"
    delivery = Delivery(**envelope(refund.refund_id))
    with pytest.raises(PaymentProviderNotConfiguredError):
        await handle_refund_delivery(delivery, factory, provider=harness.provider)
    assert delivery.settled == "abandon"
    await handle_refund_delivery(delivery, factory, provider=harness.provider)
    assert delivery.settled == "complete" and len(harness.calls) == 1
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "INITIATION_UNCERTAIN"


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b'{"refund_id":"not-uuid"}',
        b'{"refund_id":"x","phone":"forbidden"}',
        b"x" * 513,
    ],
)
async def test_malformed_refund_message_dead_letters_without_inbox_or_provider(
    database_session_factory, refund_harness, body
):
    delivery = Delivery(message_id=str(new_uuid7()), message_type="RefundRequested", body=body)
    await handle_refund_delivery(
        delivery, database_session_factory, provider=refund_harness.provider
    )
    assert delivery.settled == "dead_letter"
    assert not refund_harness.calls
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 0


@pytest.mark.parametrize(
    "kind,status,expected",
    [
        ("refund.created", "pending", "SUBMITTED"),
        ("refund.processed", "processed", "SUCCEEDED"),
        ("refund.failed", "failed", "FAILED"),
    ],
)
async def test_refund_webhooks_correlate_by_provider_or_internal_reference(
    database_session_factory, refund_harness, kind, status, expected
):
    factory, harness = database_session_factory, refund_harness
    refund, attempt = await refund_intent(factory, harness)
    harness.mode = "pending"
    await process_refund_message(factory, **envelope(refund.refund_id), provider=harness.provider)
    provider_id = "rfnd_" + refund.refund_id.hex
    entity = {
        "entity": "refund",
        "id": provider_id,
        "payment_id": "pay_" + attempt.payment_attempt_id.hex,
        "amount": 100,
        "currency": "INR",
        "status": status,
        "notes": {},
    }
    body = json.dumps(
        {"entity": "event", "event": kind, "payload": {"refund": {"entity": entity}}}
    ).encode()
    first = await harness.process(factory, body, "event_Refund")
    replay = await harness.process(factory, body, "event_Refund")
    assert first.refund_id == refund.refund_id and first.processing_status == "PROCESSED"
    assert first.payment_provider_event_id == replay.payment_provider_event_id
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == expected
        assert (await session.get(Payment, refund.payment_id)).status == "SUCCEEDED"


@pytest.mark.parametrize(
    "mismatch", ["amount", "currency", "payment", "identity", "unknown_internal"]
)
async def test_refund_webhook_conflicting_facts_preserve_intent(
    database_session_factory, refund_harness, mismatch
):
    factory, harness = database_session_factory, refund_harness
    refund, attempt = await refund_intent(factory, harness)
    fields = {
        "entity": "refund",
        "id": "rfnd_Test",
        "payment_id": "pay_" + attempt.payment_attempt_id.hex,
        "amount": 100,
        "currency": "INR",
        "status": "processed",
        "notes": {"tirodhan_refund_id": str(refund.refund_id)},
    }
    if mismatch in {"identity", "unknown_internal"}:
        async with factory() as session, session.begin():
            other = await create_refund(
                session,
                payment_id=refund.payment_id,
                payment_attempt_id=attempt.payment_attempt_id,
                amount_minor=100,
                reason_code="OPERATIONS_ADJUSTMENT",
                idempotency_key="other",
                idempotency_expires_at=utc_now() + timedelta(hours=1),
            )
            other.provider_refund_id = "rfnd_Test"
            if mismatch == "unknown_internal":
                fields["notes"] = {"tirodhan_refund_id": str(new_uuid7())}
    else:
        fields[{"amount": "amount", "currency": "currency", "payment": "payment_id"}[mismatch]] = {
            "amount": 101,
            "currency": "USD",
            "payment": "pay_Other",
        }[mismatch]
    body = json.dumps(
        {"entity": "event", "event": "refund.processed", "payload": {"refund": {"entity": fields}}}
    ).encode()
    event = await harness.process(factory, body, "event_Conflict")
    assert event.processing_status == "RECONCILIATION_REQUIRED"
    async with factory() as session:
        durable = await session.get(Refund, refund.refund_id)
        assert durable.status == "PENDING" and durable.provider_refund_id is None
