"""Actual PostgreSQL effects across queue, provider, lease and accounting boundaries."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
import test_financial_reconciliation as financial_test_support
from alembic import command as migration_command
from alembic.config import Config
from razorpay_helpers import booking, payment_body, process, settings
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from test_financial_reconciliation import (  # noqa: F401
    POLICY,
    counts,
    extra_refund,
    inquiry,
)

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.payments.inventory import InventoryPolicy, discover_inventory
from tirodhan.modules.payments.models import (
    CapturedCharge,
    FinancialException,
    FinancialScanCheckpoint,
    Payment,
    PaymentAttempt,
    PaymentProviderEvent,
    Refund,
    RefundObligation,
    SettlementEvidence,
)
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.payments.reconciliation import reconcile_batch
from tirodhan.modules.payments.refunds import RefundConflictError, execute_refund_provider_call
from tirodhan.modules.payments.settlement import (
    ExpectedSettlement,
    SettlementMovement,
    check_expected_settlement,
    reconcile_settlement_day,
    record_settlement,
)
from tirodhan.modules.payments.webhook_queue import handle_webhook_delivery, webhook_message
from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
harness = financial_test_support.harness


class Delivery:
    def __init__(self, message):
        self.message_id, self.message_type, self.body = (
            message.message_id,
            message.message_type,
            message.body,
        )
        self.completed = self.abandoned = self.dead_lettered = 0
        self.lose_ack = False

    async def complete(self):
        self.completed += 1
        if self.lose_ack:
            raise RuntimeError("synthetic lost broker acknowledgement")

    async def abandon(self):
        self.abandoned += 1

    async def dead_letter(self):
        self.dead_lettered += 1


async def queued(h, body, identity="evt_queue"):
    from razorpay_helpers import signed

    observation = await h[4].authenticate_webhook(raw_body=body, headers=signed(body, identity))
    return Delivery(webhook_message(observation, body))


async def deliver(h, delivery):
    await handle_webhook_delivery(
        delivery,
        h[0],
        account_key=h[4].account_key,
        planning_lead_time_minutes=30,
        command_ttl_seconds=86400,
    )


async def test_queue_lost_ack_replay_commits_one_acceptance_and_inbox(harness):
    h = harness
    delivery = await queued(h, payment_body(h[3]))
    delivery.lose_ack = True
    with pytest.raises(RuntimeError, match="lost broker"):
        await deliver(h, delivery)
    delivery.lose_ack = False
    await asyncio.gather(deliver(h, delivery), deliver(h, delivery))
    assert await counts(h[0], CapturedCharge) == await counts(h[0], PaymentProviderEvent) == 1
    assert await counts(h[0], InboxMessage) == 1
    async with h[0]() as session:
        assert (await session.get(CollectionRequest, h[2].request.request_id)).status == "ACCEPTED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "CollectionRequestAccepted")
            )
            == 1
        )


async def test_queue_failure_before_commit_rolls_back_evidence_inbox_and_money(
    harness, database_engine
):
    h = harness
    delivery = await queued(h, payment_body(h[3]))

    def interrupt(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO captured_charge"):
            raise RuntimeError("synthetic crash before financial commit")

    event.listen(database_engine.sync_engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="before financial"):
            await deliver(h, delivery)
    finally:
        event.remove(database_engine.sync_engine, "before_cursor_execute", interrupt)
    assert delivery.abandoned == 1 and delivery.completed == 0
    assert (
        await counts(h[0], CapturedCharge)
        == await counts(h[0], PaymentProviderEvent)
        == await counts(h[0], InboxMessage)
        == 0
    )
    await deliver(h, delivery)
    assert await counts(h[0], CapturedCharge) == 1


@pytest.mark.parametrize("problem", ["oversize", "provenance", "account", "type", "hash"])
async def test_queue_poison_does_not_touch_financial_database(harness, problem):
    h = harness
    delivery = await queued(h, payment_body(h[3]))
    data = json.loads(delivery.body)
    if problem == "oversize":
        delivery.body = b"x" * 4097
    elif problem == "type":
        delivery.message_type = "RefundRequested"
    elif problem == "hash":
        delivery.message_id = "different"
    else:
        if problem == "provenance":
            data["provenance"] = "customer_callback"
        else:
            data["facts"]["provider_account_key"] = "0" * 64
        delivery.body = json.dumps(data).encode()
    await deliver(h, delivery)
    assert delivery.dead_lettered == 1 and delivery.completed == 0
    assert await counts(h[0], PaymentProviderEvent) == await counts(h[0], InboxMessage) == 0


async def test_same_event_id_changed_hash_retains_contradiction_and_blocks_charge(harness):
    h = harness
    await deliver(h, await queued(h, payment_body(h[3])))
    await deliver(h, await queued(h, payment_body(h[3], amount=501)))
    await deliver(h, await queued(h, payment_body(h[3], amount=501)))
    assert await counts(h[0], CapturedCharge) == 1
    assert await counts(h[0], PaymentProviderEvent) == 2
    async with h[0]() as session:
        original = await session.scalar(
            select(PaymentProviderEvent).where(
                PaymentProviderEvent.external_event_id == "evt_queue"
            )
        )
        contradiction = await session.scalar(
            select(PaymentProviderEvent).where(
                PaymentProviderEvent.contradicted_event_id == original.payment_provider_event_id
            )
        )
        assert original.amount_minor == 500 and contradiction.amount_minor == 501
        assert contradiction.failure_code == "EVENT_PAYLOAD_CONFLICT"
        assert (
            await session.scalar(select(FinancialException.reason_code)) == "EVENT_PAYLOAD_CONFLICT"
        )


async def test_unmatched_authenticated_event_replays_when_mapping_becomes_available(harness):
    h = harness
    body = payment_body(h[3], order_id="order_later")
    message = await queued(h, body)
    await deliver(h, message)
    async with h[0]() as session, session.begin():
        attempt = await session.get(PaymentAttempt, h[3].payment_attempt_id)
        attempt.provider_order_id = "order_later"
    await deliver(h, message)
    assert await counts(h[0], CapturedCharge) == 1
    assert await counts(h[0], PaymentProviderEvent) == 1


async def test_lease_takeover_during_get_stale_release_does_not_clobber_or_stop_next_work(harness):
    h = harness
    factory, _, _, attempt, provider, gateway = h
    # Old pending observation returns after a different worker has verified capture.
    gateway.payments[0]["status"], gateway.payments[0]["captured"] = "authorized", False
    stale = await provider.inquire_payment(
        attempt_id=attempt.payment_attempt_id,
        order_id=attempt.provider_order_id,
        amount_minor=500,
        currency="INR",
    )
    entered, resume = asyncio.Event(), asyncio.Event()
    original = provider.inquire_payment

    async def delayed(**kwargs):
        if kwargs["attempt_id"] != attempt.payment_attempt_id:
            return []
        entered.set()
        await resume.wait()
        return stale

    provider.inquire_payment = delayed
    task = asyncio.create_task(reconcile_batch(factory, provider, replace(POLICY, batch_size=2)))
    await asyncio.wait_for(entered.wait(), 5)
    async with factory() as session, session.begin():
        current = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        current.claim_until = current.next_check_at = utc_now() - timedelta(seconds=1)
    gateway.payments[0]["status"], gateway.payments[0]["captured"] = "captured", True
    second = RazorpayProvider(settings(), client=provider._client)
    assert await reconcile_batch(factory, second, replace(POLICY, batch_size=1)) == 1
    unrelated_id = new_uuid7()
    async with factory() as session, session.begin():
        session.add(
            PaymentAttempt(
                payment_attempt_id=unrelated_id,
                payment_id=attempt.payment_id,
                provider="RAZORPAY",
                provider_order_id="order_unrelated",
                provider_idempotency_key="unrelated-operation",
                status="PENDING",
                provider_account_key=provider.account_key,
                next_check_at=utc_now() - timedelta(seconds=1),
            )
        )
    resume.set()
    await asyncio.wait_for(task, 10)
    provider.inquire_payment = original
    async with factory() as session:
        current = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        assert current.status == "SUCCEEDED" and current.check_count == 1
        assert current.next_check_at is None and current.claim_token is None
        assert (await session.get(Payment, current.payment_id)).status == "SUCCEEDED"
        assert (await session.get(PaymentAttempt, unrelated_id)).check_count == 1


async def test_batch_one_oldest_due_payment_is_not_starved_by_refund_population(harness):
    h = harness
    refund = await extra_refund(h)
    async with h[0]() as session, session.begin():
        attempt = await session.get(PaymentAttempt, h[3].payment_attempt_id)
        attempt.next_check_at = utc_now() - timedelta(hours=2)
        operation = await session.get(Refund, refund.refund_id)
        operation.next_check_at = utc_now() - timedelta(hours=1)
    assert await reconcile_batch(h[0], h[4], replace(POLICY, batch_size=1)) == 1
    async with h[0]() as session:
        assert (await session.get(PaymentAttempt, h[3].payment_attempt_id)).check_count == 1
        assert (await session.get(Refund, refund.refund_id)).check_count == 0


@pytest.mark.parametrize("window", [None, 30])
async def test_aged_or_unconfirmed_native_replay_never_posts_another_operation(harness, window):
    h = harness
    refund = await extra_refund(h)
    async with h[0]() as session, session.begin():
        current = await session.get(Refund, refund.refund_id)
        current.status = "INITIATION_UNCERTAIN"
        current.processing_started_at = utc_now() - timedelta(days=2)
    provider = RazorpayProvider(
        settings(razorpay_refund_replay_window_seconds=window), client=h[4]._client
    )
    with pytest.raises(RefundConflictError, match="replay protection"):
        await execute_refund_provider_call(h[0], refund.refund_id, provider)
    assert not any(method == "POST" and path.endswith("/refund") for method, path in h[5].calls)
    async with h[0]() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "INITIATION_UNCERTAIN"
        assert (
            await session.scalar(
                select(FinancialException.reason_code).where(
                    FinancialException.refund_id == refund.refund_id
                )
            )
            == "REFUND_REPLAY_RETENTION_UNCONFIRMED"
        )


async def test_uncertain_post_recovered_by_get_has_one_external_refund(harness):
    h = harness
    refund = await extra_refund(h)
    h[5].refund_timeout = True
    await execute_refund_provider_call(h[0], refund.refund_id, h[4])
    h[5].refund_timeout = False
    await execute_refund_provider_call(h[0], refund.refund_id, h[4])
    assert len(h[5].refunds) == 1
    assert sum(method == "POST" and path.endswith("/refund") for method, path in h[5].calls) == 1
    async with h[0]() as session:
        current = await session.get(Refund, refund.refund_id)
        assert current.status == "SUBMITTED" and current.provider_refund_id is not None
        assert current.provider_idempotency_key == f"rf_{refund.refund_id.hex}"


async def test_account_inventory_finds_missed_second_capture_and_dashboard_refund(harness):
    h = harness
    await inquiry(h)
    now = int(utc_now().timestamp())
    payments = [
        {**h[5].payments[0], "created_at": now - 180},
        {**h[5].payments[0], "id": "pay_missed", "created_at": now - 180},
    ]
    external = dict(
        entity="refund",
        id="rfnd_dashboard",
        payment_id="pay_missed",
        amount=500,
        currency="INR",
        status="processed",
        receipt=None,
        notes={},
        created_at=now - 180,
    )

    async def transport(request):
        assert request.method == "GET"
        items = payments if request.url.path == "/v1/payments" else [external]
        skip = int(request.url.params["skip"])
        page = items[skip : skip + 100]
        return httpx.Response(200, json=dict(entity="collection", count=len(page), items=page))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        policy = InventoryPolicy(now - 3600, 3600, 120, 60, 2)
        for kind in ("PAYMENTS", "REFUNDS"):
            await discover_inventory(h[0], provider, policy, POLICY, kind)
    assert await counts(h[0], CapturedCharge) == 2
    assert (
        await counts(h[0], Refund) == 1
    )  # Only approved extra-charge intent; no fake Dashboard execution.
    async with h[0]() as session:
        obligation = await session.scalar(select(RefundObligation))
        assert obligation.payout_blocked
        assert await session.scalar(
            select(FinancialException.reason_code).where(
                FinancialException.reason_code == "EXTERNAL_REFUND_UNMAPPED"
            )
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "CollectionRequestAccepted")
            )
            == 1
        )


def movement(entity="pay_success", kind="payment", **patch):
    return SettlementMovement.parse(
        dict(
            entity_id=entity,
            type=kind,
            settlement_id="setl_daily",
            payment_id=None,
            amount=500,
            currency="INR",
            debit=0,
            credit=470,
            fee=30,
            tax=5,
            settled=True,
            on_hold=False,
            created_at=1700000000,
            settled_at=1700100000,
        )
        | patch
    )


async def test_settlement_gross_fee_tax_replay_adjustment_and_unknown_retained(harness):
    h = harness
    await inquiry(h)
    item = movement()
    first = await record_settlement(h[0], h[4].account_key, item, fee_includes_tax=True)
    assert first.classification == "MATCHED"
    assert first.credit_minor == 470 and first.fee_minor == 30 and first.tax_minor == 5
    repeated = await record_settlement(h[0], h[4].account_key, item, fee_includes_tax=True)
    assert repeated.settlement_evidence_id == first.settlement_evidence_id
    changed = await record_settlement(
        h[0], h[4].account_key, replace(item, fee_minor=31), fee_includes_tax=True
    )
    assert changed.classification == "SETTLEMENT_MISMATCH"
    unknown = await record_settlement(h[0], h[4].account_key, movement("pay_unknown"))
    assert unknown.classification == "SETTLEMENT_UNKNOWN_PAYMENT"
    adjustment = await record_settlement(h[0], h[4].account_key, movement("adj_late", "adjustment"))
    assert adjustment.classification == "SETTLEMENT_ADJUSTMENT_REVIEW"
    assert await counts(h[0], SettlementEvidence) == 4
    async with h[0]() as session:
        case = await session.scalar(
            select(FinancialException).where(
                FinancialException.reason_code == "SETTLEMENT_MISMATCH"
            )
        )
        assert case.settlement_evidence_id == changed.settlement_evidence_id


async def test_missing_settlement_requires_verified_membership_due_and_coverage(harness):
    h = harness
    await inquiry(h)
    async with h[0]() as session:
        charge = await session.scalar(select(CapturedCharge))
    future = ExpectedSettlement(
        charge.captured_charge_id, None, "setl_expected", utc_now() + timedelta(days=1)
    )
    due = replace(future, due_at=utc_now() - timedelta(days=1))
    assert (
        await check_expected_settlement(h[0], h[4].account_key, [future], coverage_verified=True)
        == 0
    )
    assert (
        await check_expected_settlement(h[0], h[4].account_key, [due], coverage_verified=False) == 0
    )
    assert (
        await check_expected_settlement(h[0], h[4].account_key, [due], coverage_verified=True) == 1
    )
    assert (
        await check_expected_settlement(h[0], h[4].account_key, [due], coverage_verified=True) == 1
    )
    assert await counts(h[0], FinancialException) == 1


async def test_dispute_after_refund_preserves_capture_and_blocks_double_credit(harness):
    h = harness
    operation = await extra_refund(h)
    await execute_refund_provider_call(h[0], operation.refund_id, h[4])
    entity = dict(
        entity="dispute",
        id="disp_chargeback",
        payment_id="pay_extra",
        amount=500,
        currency="INR",
        status="lost",
        amount_deducted=500,
    )
    body = json.dumps(
        dict(event="payment.dispute.lost", payload=dict(dispute=dict(entity=entity)))
    ).encode()
    await process(h[0], h[4], body, "evt_dispute")
    await process(h[0], h[4], body, "evt_dispute")
    assert await counts(h[0], CapturedCharge) == 2 and await counts(h[0], Refund) == 1
    async with h[0]() as session:
        assert (await session.get(RefundObligation, operation.refund_obligation_id)).payout_blocked
        assert set(await session.scalars(select(FinancialException.reason_code))) >= {
            "PAYMENT_DISPUTE",
            "REFUND_DISPUTE_DOUBLE_CREDIT_RISK",
        }


async def test_database_capture_audit_and_submitted_request_facts_are_immutable(harness):
    h = harness
    operation = await extra_refund(h)
    await execute_refund_provider_call(h[0], operation.refund_id, h[4])
    await inquiry(h, manager=True)
    await record_settlement(h[0], h[4].account_key, movement())
    for sql in (
        "UPDATE captured_charge SET amount_minor=amount_minor+1",
        "UPDATE captured_charge SET currency='USD'",
        "UPDATE refund SET provider_idempotency_key='different_native_key'",
        "UPDATE financial_audit SET action='REWRITTEN'",
        "DELETE FROM settlement_evidence",
        "UPDATE payment_provider_event SET currency='USD'",
        "DELETE FROM payment_provider_event WHERE refund_id IS NULL",
    ):
        with pytest.raises(IntegrityError):
            async with h[0]() as session, session.begin():
                await session.execute(text(sql))
    async with h[0]() as session:
        assert (await session.get(Refund, operation.refund_id)).amount_minor == 500


@pytest.mark.parametrize("source", ["WEBHOOK", "SETTLEMENT"])
async def test_external_refund_before_extra_capture_blocks_payout_without_losing_capture(
    harness, source
):
    h = harness
    await inquiry(h)
    entity = dict(
        entity="refund",
        id="rfnd_externalbefore",
        payment_id="pay_beforecapture",
        amount=500,
        currency="INR",
        status="processed",
        notes={},
        receipt=None,
    )
    raw = json.dumps(
        dict(event="refund.processed", payload=dict(refund=dict(entity=entity)))
    ).encode()
    if source == "WEBHOOK":
        first = await process(h[0], h[4], raw, "evt_external_before")
        assert first.processing_status == "UNMATCHED"
    else:
        first = await record_settlement(
            h[0],
            h[4].account_key,
            movement(
                "rfnd_externalbefore",
                "refund",
                payment_id="pay_beforecapture",
                debit=500,
                credit=0,
                fee=0,
                tax=0,
            ),
        )
        assert first.classification == "SETTLEMENT_UNKNOWN_REFUND"
    capture = payment_body(h[3], payment_id="pay_beforecapture")
    await process(h[0], h[4], capture, "evt_capture_after")
    await process(h[0], h[4], capture, "evt_capture_after")
    assert await counts(h[0], CapturedCharge) == 2
    assert await counts(h[0], Refund) == 0
    async with h[0]() as session:
        assert (await session.scalar(select(RefundObligation))).payout_blocked
        if source == "WEBHOOK":
            assert (
                await session.get(PaymentProviderEvent, first.payment_provider_event_id)
            ).processing_status == "RECONCILIATION_REQUIRED"
        assert await session.scalar(
            select(FinancialException.financial_exception_id).where(
                FinancialException.reason_code
                == (
                    "EXTERNAL_REFUND_UNMAPPED"
                    if source == "WEBHOOK"
                    else "SETTLEMENT_UNKNOWN_REFUND"
                )
            )
        )


@pytest.mark.parametrize("legacy", [False, True])
async def test_two_real_sessions_cannot_overreserve_known_charge_with_legacy_mapping(
    harness, legacy
):
    h = harness
    await inquiry(h)
    async with h[0]() as session, session.begin():
        charge = await session.scalar(select(CapturedCharge))
        obligation = RefundObligation(
            captured_charge_id=charge.captured_charge_id,
            amount_minor=500,
            reason_code="CUSTOMER_CANCELLATION",
        )
        session.add(obligation)
        await session.flush()
    flushed, release, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids = set()

    async def insert(first):
        async with h[0]() as session, session.begin():
            pids.add(await session.scalar(text("SELECT pg_backend_pid()")))
            identity = new_uuid7()
            session.add(
                Refund(
                    refund_id=identity,
                    payment_id=charge.payment_id,
                    payment_attempt_id=charge.payment_attempt_id,
                    amount_minor=300,
                    currency="INR",
                    reason_code="CUSTOMER_CANCELLATION",
                    status="PENDING",
                    provider="RAZORPAY",
                    provider_idempotency_key=f"rf_{identity.hex}",
                    captured_charge_id=None if legacy and not first else charge.captured_charge_id,
                    refund_obligation_id=None
                    if legacy and not first
                    else obligation.refund_obligation_id,
                )
            )
            if not first:
                entered.set()
            await session.flush()
            if first:
                flushed.set()
                await release.wait()
        return True

    first = asyncio.create_task(insert(True))
    await asyncio.wait_for(flushed.wait(), 5)
    second = asyncio.create_task(insert(False))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        await asyncio.wait_for(asyncio.shield(second), 0.2)
    except asyncio.TimeoutError:
        pass  # Correct charge locking blocks the second session until first commit.
    finally:
        release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert len(pids) == 2
    assert sum(r is True for r in results) == 1
    assert sum(isinstance(r, IntegrityError) for r in results) == 1
    assert await counts(h[0], Refund) == 1


async def test_successful_attempt_owner_fk_and_no_financial_cascading_delete(harness):
    h = harness
    _, other = await booking(h[0])
    with pytest.raises(IntegrityError):
        async with h[0]() as session, session.begin():
            await session.execute(
                text("UPDATE payment SET successful_attempt_id=:attempt WHERE payment_id=:payment"),
                dict(attempt=h[3].payment_attempt_id, payment=other.payment.payment_id),
            )
    async with h[0]() as session:
        rows = await session.execute(
            text("""
          SELECT conname,confdeltype FROM pg_constraint
          WHERE contype='f' AND conrelid IN
            ('payment'::regclass,'payment_attempt'::regclass,'captured_charge'::regclass,
             'refund'::regclass,'refund_obligation'::regclass,'financial_audit'::regclass,
             'financial_exception'::regclass,'settlement_evidence'::regclass,
             'payment_provider_event'::regclass)
        """)
        )
        constraints = list(rows)
        assert len(constraints) >= 25
        assert all(rule != "c" for _, rule in constraints)


async def test_new_migration_preserves_submitted_legacy_wire_identity_and_financial_history(
    harness,
):
    h = harness
    operation = await extra_refund(h)
    await execute_refund_provider_call(h[0], operation.refund_id, h[4])
    await inquiry(h, manager=True)
    async with h[0]() as session:
        before = (await session.get(Refund, operation.refund_id)).provider_refund_id
    tables = (
        Payment,
        PaymentAttempt,
        CapturedCharge,
        RefundObligation,
        Refund,
        PaymentProviderEvent,
    )
    counts_before = [await counts(h[0], model) for model in tables]
    try:
        await asyncio.to_thread(
            migration_command.downgrade, Config("alembic.ini"), "0020_financial_reconciliation"
        )
        async with h[0]() as session, session.begin():
            await session.execute(
                text("UPDATE refund SET provider_idempotency_key=:key WHERE refund_id=:id"),
                dict(key=f"refund:{operation.refund_id}", id=operation.refund_id),
            )
    finally:
        await asyncio.to_thread(migration_command.upgrade, Config("alembic.ini"), "head")
    assert [await counts(h[0], model) for model in tables] == counts_before
    async with h[0]() as session:
        current = await session.get(Refund, operation.refund_id)
        assert current.provider_idempotency_key == f"rf_{operation.refund_id.hex}"
        assert current.provider_refund_id == before and current.amount_minor == 500
        assert current.status == "SUBMITTED" and current.processing_started_at is not None
    await execute_refund_provider_call(h[0], operation.refund_id, h[4])
    assert sum(method == "POST" and path.endswith("/refund") for method, path in h[5].calls) == 1


async def test_inventory_full_page_resumes_and_new_record_between_passes_is_retained(harness):
    h = harness
    await inquiry(h)
    now = int(utc_now().timestamp())
    # Unrelated captures cannot be matched to an order by amount alone.
    items = [
        dict(
            h[5].payments[0], id=f"pay_unknown{i}", order_id="order_external", created_at=now - 180
        )
        for i in range(100)
    ]
    calls = []

    def transport(request):
        assert request.method == "GET"
        assert h[5].engine.pool.checkedout() == 0
        offset = int(request.url.params["skip"])
        calls.append(offset)
        return httpx.Response(
            200,
            json=dict(
                entity="collection",
                count=len(items[offset : offset + 100]),
                items=items[offset : offset + 100],
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        policy = InventoryPolicy(now - 3600, 3600, 120, 60, 1)
        await discover_inventory(h[0], provider, policy, POLICY, "PAYMENTS")
        async with h[0]() as session:
            checkpoint = await session.scalar(select(FinancialScanCheckpoint))
            assert checkpoint.page_offset == 100 and checkpoint.last_exhausted_at is None
        items.insert(0, dict(items[0], id="pay_newduringpass"))
        await discover_inventory(h[0], provider, policy, POLICY, "PAYMENTS")
        await discover_inventory(h[0], provider, policy, POLICY, "PAYMENTS")
        await discover_inventory(h[0], provider, policy, POLICY, "PAYMENTS")
        async with h[0]() as session:
            checkpoint = await session.scalar(select(FinancialScanCheckpoint))
            assert checkpoint.last_exhausted_at is None
        await discover_inventory(h[0], provider, policy, POLICY, "PAYMENTS")
        await discover_inventory(h[0], provider, policy, POLICY, "PAYMENTS")
    assert calls == [0, 100, 0, 100, 0, 100]
    assert await counts(h[0], CapturedCharge) == 1
    async with h[0]() as session:
        checkpoint = await session.scalar(select(FinancialScanCheckpoint))
        assert checkpoint.last_exhausted_at is not None
        assert await session.scalar(
            select(PaymentProviderEvent.payment_provider_event_id).where(
                PaymentProviderEvent.provider_payment_id == "pay_newduringpass",
                PaymentProviderEvent.processing_status == "UNMATCHED",
            )
        )


async def test_settlement_exact_1000_page_resumes_and_fee_rule_reassessment_preserves_prior(
    harness,
):
    h = harness
    await inquiry(h)
    item = movement()
    gross_only = await record_settlement(h[0], h[4].account_key, item)
    net_checked = await record_settlement(h[0], h[4].account_key, item, fee_includes_tax=False)
    assert gross_only.classification == "MATCHED_GROSS"
    assert net_checked.classification == "SETTLEMENT_MISMATCH"
    assert gross_only.settlement_evidence_id != net_checked.settlement_evidence_id
    fields = item.fields()
    page = [
        dict(
            entity_id=f"pay_external{i}",
            type="payment",
            settlement_id="setl_daily",
            payment_id=None,
            amount=500,
            currency="INR",
            debit=0,
            credit=470,
            fee=30,
            tax=5,
            settled=True,
            on_hold=False,
            created_at=1700000000,
            settled_at=1700100000,
        )
        for i in range(1000)
    ]
    calls = []

    def transport(request):
        assert request.method == "GET" and request.url.path == "/v1/settlements/recon"
        assert request.url.params["count"] == "1000"
        offset = int(request.url.params["skip"])
        calls.append(offset)
        items = page[offset : offset + 1000]
        return httpx.Response(200, json=dict(entity="collection", count=len(items), items=items))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        for _ in range(2):
            await reconcile_settlement_day(
                h[0], provider, item.provider_created_at.date(), page_budget=1, lease_seconds=120
            )
    assert calls == [0, 1000]
    assert fields["amount_minor"] == 500
    assert await counts(h[0], SettlementEvidence) == 1002
