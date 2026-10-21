"""Regression proofs for the adversarial review, using the existing real DB harness."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from alembic import command as migration_command
from alembic.config import Config
from razorpay_helpers import initiate, payment_body, process, settings, signed
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_financial_correctness_closure import movement
from test_financial_reconciliation import POLICY, counts, extra_refund, inquiry
from test_financial_reconciliation import harness as harness
from test_razorpay_refunds import Refunds
from test_razorpay_refunds import body as refund_body
from test_razorpay_refunds import intent as legacy_intent

from tirodhan.api.routes.manager_finance import audit_inventory, detail, inventory
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.expiry import expire_pending_collection_requests
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.payments.accounting import open_exception
from tirodhan.modules.payments.inventory import InventoryPolicy, discover_inventory
from tirodhan.modules.payments.manager_recovery import recover_financial_record
from tirodhan.modules.payments.models import (
    CapturedCharge,
    FinancialAudit,
    FinancialException,
    Payment,
    PaymentAttempt,
    Refund,
    RefundObligation,
)
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.payments.reconciliation import (
    approve_refund,
    escalate_unresolved_cases,
    inquire_target,
    reconcile_batch,
)
from tirodhan.modules.payments.refunds import RefundConflictError, execute_refund_provider_call
from tirodhan.modules.payments.settlement import (
    ExpectedSettlement,
    check_expected_settlement,
    record_settlement,
)
from tirodhan.modules.reliability.publisher import publish_outbox_batch

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_failed_webhook_is_not_order_finality(harness):
    factory, user, result, attempt, provider, gateway = harness
    gateway.payments = [
        {**gateway.payments[0], "status": "failed", "captured": False},
        {**gateway.payments[0], "id": "pay_pending", "status": "authorized", "captured": False},
    ]
    await process(factory, provider, payment_body(attempt, event_type="payment.failed"))
    async with factory() as session:
        current = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        assert current.status == "PENDING"
        assert current.next_check_at is not None
    resumed = await initiate(factory, user, result, provider, key="still-unresolved")
    assert resumed.payment_attempt_id == attempt.payment_attempt_id
    assert resumed.checkout_ready_until is None
    await process(
        factory,
        provider,
        payment_body(attempt, payment_id="pay_pending", event_type="payment.authorized"),
        event_id="evt_authorized",
    )
    await inquiry(harness)
    async with factory() as session:
        assert (await session.get(PaymentAttempt, attempt.payment_attempt_id)).status == "PENDING"


async def test_approval_retains_unpaid_liability_in_operations(harness):
    factory, user, result, _, provider, _ = harness
    async with factory() as session, session.begin():
        request = await session.get(CollectionRequest, result.request.request_id)
        request.created_at = utc_now() - timedelta(hours=1)
        request.payment_expires_at = utc_now() - timedelta(seconds=1)
        await expire_pending_collection_requests(session, utc_now())
    await inquiry(harness)
    async with factory() as session:
        charge = await session.scalar(select(CapturedCharge))
    await approve_refund(
        factory,
        provider,
        actor=user.user_id,
        command_id=new_uuid7(),
        charge_id=charge.captured_charge_id,
        policy=POLICY,
    )
    async with factory() as session:
        cases = list(await session.scalars(select(FinancialException)))
        assert any(c.status == "OPEN" for c in cases)
        queue = await inventory(manager=None, session=session)
        assert charge.payment_id in [r["payment_id"] for r in queue["payments"]]


async def test_verified_recurrence_reactivates_case_and_attention(harness):
    factory, _, _, _, _, _ = harness
    async with factory() as session, session.begin():
        payment = await session.scalar(select(Payment).with_for_update())
        case = await open_exception(session, payment, "PAYMENT_UNRESOLVED")
        case.status = "RESOLVED"
        case.resolved_at = utc_now()
    async with factory() as session, session.begin():
        payment = await session.scalar(select(Payment).with_for_update())
        case = await open_exception(session, payment, "PAYMENT_UNRESOLVED")
        assert case.status == "OPEN"
        assert case.resolved_at is None


async def test_retiring_secret_accepts_legitimate_retry_only_until_deadline():
    body = b'{"event":"unhandled.event"}'
    headers = signed(body)
    headers["x-razorpay-signature"] = hmac.new(b"retiring-secret", body, hashlib.sha256).hexdigest()
    config = settings(
        razorpay_webhook_previous_secret="retiring-secret",
        razorpay_webhook_previous_secret_until=utc_now() + timedelta(hours=1),
    )
    async with httpx.AsyncClient() as client:
        provider = RazorpayProvider(config, client=client)
        await provider.authenticate_webhook(raw_body=body, headers=headers)


async def test_refund_contradiction_holds_existing_execution(harness):
    from tirodhan.modules.customer_reads.finance import load_financials
    from tirodhan.modules.customer_reads.projections import refund_projection

    factory, _, _, _, provider, gateway = harness
    refund = await extra_refund(harness)
    await process(factory, provider, refund_body(refund, payment_id="pay_extra", amount=499))
    async with factory() as session:
        obligation = await session.get(RefundObligation, refund.refund_obligation_id)
        assert obligation.payout_blocked
        request = await session.get(CollectionRequest, harness[2].request.request_id)
        snapshot = (await load_financials(session, [request]))[request.request_id]
        assert (
            refund_projection(refund, held=refund.refund_id in snapshot.held_refunds).status
            == "CONFIRMING"
        )
    with pytest.raises(RefundConflictError):
        await execute_refund_provider_call(factory, refund.refund_id, provider)
    assert not gateway.refunds


async def test_legacy_failed_refund_records_verified_processed_truth(database_session_factory):
    factory = database_session_factory
    refund = await legacy_intent(factory)
    async with httpx.AsyncClient() as client:
        provider = RazorpayProvider(settings(), client=client)
        await process(factory, provider, refund_body(refund, kind="refund.failed"), "evt_failed")
        await process(factory, provider, refund_body(refund), "evt_processed")
    async with factory() as session:
        row = await session.get(Refund, refund.refund_id)
        assert row.status == "SUCCEEDED"
        assert row.completed_at is not None


async def test_complete_exception_traversal_and_repeated_attention_are_bounded(harness):
    factory, _, _, attempt, _, _ = harness
    async with factory() as session, session.begin():
        payment = await session.get(Payment, attempt.payment_id, with_for_update=True)
        for index in range(111):
            case = await open_exception(session, payment, f"REVIEW_{index}")
            case.detected_at = utc_now() - timedelta(hours=2)
    async with factory() as session:
        first = await detail(attempt.payment_id, None, session, section="exceptions", limit=100)
        assert len(first["exceptions"]) == 100
        second = await detail(
            attempt.payment_id,
            None,
            session,
            section="exceptions",
            after=first["cursors"]["exceptions"],
            limit=100,
        )
        assert len(second["exceptions"]) == 11 and second["cursors"]["exceptions"] is None
        assert (
            len({c["financial_exception_id"] for c in first["exceptions"] + second["exceptions"]})
            == 111
        )
    assert await escalate_unresolved_cases(factory, replace(POLICY, batch_size=1000)) == 111
    assert await escalate_unresolved_cases(factory, replace(POLICY, batch_size=1000)) == 0


@pytest.mark.parametrize("harness", ["PRE_ADMINISTRATION"], indirect=True)
async def test_new_recovery_audit_preserves_history_and_refuses_lossy_downgrade(harness):
    factory, user, _, attempt, provider, _ = harness
    refund = await extra_refund(harness)
    audit = await recover_financial_record(
        factory,
        provider,
        actor=user.user_id,
        command_id=new_uuid7(),
        target_id=refund.refund_id,
        action="RECOVER_ORIGINAL",
        policy=POLICY,
    )
    with pytest.raises(DBAPIError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE financial_audit SET result_code='invented' WHERE financial_audit_id=:id"
                ),
                {"id": audit.financial_audit_id},
            )
    await asyncio.to_thread(
        migration_command.downgrade, Config("alembic.ini"), "0021_financial_correctness"
    )
    await asyncio.to_thread(migration_command.upgrade, Config("alembic.ini"), "head")
    async with factory() as session:
        stored = await session.get(FinancialAudit, audit.financial_audit_id)
        assert stored.result_code == audit.result_code and stored.command_id == audit.command_id
        assert (
            await session.get(Refund, refund.refund_id)
        ).provider_idempotency_key == refund.provider_idempotency_key


async def test_completed_delivery_recovers_after_mapping_without_transport_replay(harness):
    factory, _, _, attempt, provider, gateway = harness
    await inquiry(harness)
    await process(
        factory,
        provider,
        payment_body(attempt, payment_id="pay_later", order_id="order_later"),
        "evt_later",
    )
    async with factory() as session, session.begin():
        row = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        row.provider_order_id = "order_later"
        assert row.status == "SUCCEEDED" and row.next_check_at is None
    gateway.payments = [{**gateway.payments[0], "id": "pay_later", "order_id": "order_later"}]
    next(iter(gateway.orders.values()))["id"] = "order_later"
    await reconcile_batch(factory, provider, POLICY)
    assert await counts(factory, CapturedCharge) == 2
    assert await counts(factory, Refund) == 1
    await reconcile_batch(factory, provider, POLICY)
    assert await counts(factory, Refund) == 1


async def test_current_settlement_can_return_to_prior_facts_without_stale_assessment(harness):
    factory, _, _, _, provider, _ = harness
    await inquiry(harness)
    first = await record_settlement(
        factory, provider.account_key, movement(), fee_includes_tax=True
    )
    await record_settlement(
        factory,
        provider.account_key,
        replace(movement(), settled=False, on_hold=True),
        fee_includes_tax=True,
    )
    async with factory() as session:
        charge = await session.scalar(select(CapturedCharge))
    expected = ExpectedSettlement(
        charge.captured_charge_id, None, first.provider_settlement_id, utc_now() - timedelta(days=1)
    )
    assert (
        await check_expected_settlement(
            factory, provider.account_key, [expected], coverage_verified=True
        )
        == 1
    )
    corrected = await record_settlement(
        factory, provider.account_key, movement(), fee_includes_tax=True
    )
    assert corrected.settlement_evidence_id != first.settlement_evidence_id
    assert (
        await check_expected_settlement(
            factory, provider.account_key, [expected], coverage_verified=True
        )
        == 0
    )


async def test_exception_event_reaches_existing_operations_log(harness, caplog):
    factory, _, _, _, _, _ = harness
    async with factory() as session, session.begin():
        payment = await session.scalar(select(Payment).with_for_update())
        case = await open_exception(session, payment, "PAYMENT_UNRESOLVED")

    class NoSend:
        async def send(self, *args):
            raise AssertionError("Operations attention uses the existing logging sink")

    with caplog.at_level("WARNING"):
        assert (
            await publish_outbox_batch(
                factory, NoSend(), serviceability_entity="test", batch_size=10
            )
            == 1
        )
    assert str(case.financial_exception_id) in caplog.text


async def test_retired_window_late_capture_is_found_by_bounded_history(harness):
    factory, _, _, attempt, _, gateway = harness
    await inquiry(harness)
    now = int(utc_now().timestamp())
    present = []

    async def transport(request):
        start, end = int(request.url.params["from"]), int(request.url.params["to"])
        items = [p for p in present if start <= p["created_at"] <= end]
        return httpx.Response(200, json=dict(entity="collection", count=len(items), items=items))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        policy = InventoryPolicy(now - 400, 200, 20, 60, 2)
        await discover_inventory(factory, provider, policy, POLICY, "PAYMENTS")
        present.append({**gateway.payments[0], "id": "pay_retired", "created_at": now - 390})
        await discover_inventory(factory, provider, policy, POLICY, "PAYMENTS")
        assert await counts(factory, CapturedCharge) == 1
        for _ in range(3):
            await discover_inventory(factory, provider, policy, POLICY, "PAYMENTS", historical=True)
    assert await counts(factory, CapturedCharge) == 2
    assert await counts(factory, Refund) == 1


async def test_hold_after_claim_commits_before_post_is_rechecked(harness):
    factory, _, _, _, provider, gateway = harness
    refund = await extra_refund(harness)
    entered, release = asyncio.Event(), asyncio.Event()
    original_get = provider.inquire_refund

    async def blocked_get(**kwargs):
        entered.set()
        await release.wait()
        return await original_get(**kwargs)

    provider.inquire_refund = blocked_get
    task = asyncio.create_task(execute_refund_provider_call(factory, refund.refund_id, provider))
    await asyncio.wait_for(entered.wait(), 5)
    await process(
        factory, provider, refund_body(refund, payment_id="pay_extra", amount=499), "evt_during_get"
    )
    release.set()
    with pytest.raises(RefundConflictError):
        await asyncio.wait_for(task, 10)
    assert not gateway.refunds
    assert not any(method == "POST" and path.endswith("/refund") for method, path in gateway.calls)


async def test_claim_never_posted_can_recover_only_original_identity(harness):
    factory, user, _, _, provider, gateway = harness
    refund = await extra_refund(harness)
    started = utc_now() - timedelta(seconds=1)
    async with factory() as session, session.begin():
        row = await session.get(Refund, refund.refund_id)
        row.status, row.processing_started_at = "INITIATION_UNCERTAIN", started
    blocked = await recover_financial_record(
        factory,
        provider,
        actor=user.user_id,
        command_id=new_uuid7(),
        target_id=refund.refund_id,
        action="RECOVER_ORIGINAL",
        policy=POLICY,
    )
    assert blocked.result_code == "PROVIDER_BLOCKED"
    provider.refund_replay_window_seconds = (
        3600  # Synthetic guarantee; not production configuration.
    )
    command = new_uuid7()
    audit = await recover_financial_record(
        factory,
        provider,
        actor=user.user_id,
        command_id=command,
        target_id=refund.refund_id,
        action="RECOVER_ORIGINAL",
        policy=POLICY,
    )
    assert audit.result_code == "ORIGINAL_RECOVERY_QUEUED"
    await execute_refund_provider_call(factory, refund.refund_id, provider)
    await execute_refund_provider_call(factory, refund.refund_id, provider)
    assert len(gateway.refunds) == 1 and await counts(factory, Refund) == 1
    assert sum(m == "POST" and p.endswith("/refund") for m, p in gateway.calls) == 1
    async with factory() as session:
        row = await session.get(Refund, refund.refund_id)
        assert row.processing_started_at == started
        assert row.provider_idempotency_key == f"rf_{refund.refund_id.hex}"


async def test_native_deadline_is_rechecked_immediately_before_http(harness, monkeypatch):
    factory, _, _, _, provider, gateway = harness
    refund = await extra_refund(harness)
    provider.refund_replay_window_seconds = 3600
    async with factory() as session, session.begin():
        row = await session.get(Refund, refund.refund_id)
        row.status, row.processing_started_at = "INITIATION_UNCERTAIN", utc_now()
    original_request = provider._request

    async def delayed_request(method, path, **kwargs):
        if method == "POST":
            monkeypatch.setattr(
                "tirodhan.modules.payments.razorpay.utc_now", lambda: utc_now() + timedelta(hours=2)
            )
        return await original_request(method, path, **kwargs)

    monkeypatch.setattr(provider, "_request", delayed_request)
    await execute_refund_provider_call(factory, refund.refund_id, provider)
    assert not gateway.refunds
    assert not any(m == "POST" and p.endswith("/refund") for m, p in gateway.calls)
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "INITIATION_UNCERTAIN"


async def test_preflight_timeout_retains_debt_and_does_not_post(harness, monkeypatch):
    factory, _, _, _, provider, gateway = harness
    refund = await extra_refund(harness)

    async def unavailable(**kwargs):
        from tirodhan.modules.payments.ports import PaymentProviderUncertainError

        raise PaymentProviderUncertainError("SYNTHETIC_UNAVAILABLE")

    monkeypatch.setattr(provider, "inquire_refund", unavailable)
    with pytest.raises(RefundConflictError):
        await execute_refund_provider_call(factory, refund.refund_id, provider)
    assert not gateway.refunds
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "INITIATION_UNCERTAIN"
        assert await session.scalar(
            select(FinancialException).where(
                FinancialException.refund_id == refund.refund_id,
                FinancialException.status == "OPEN",
            )
        )
        assert (
            await session.get(RefundObligation, refund.refund_obligation_id)
        ).amount_minor == 500


@pytest.mark.parametrize("harness", ["PRE_ADMINISTRATION"], indirect=True)
async def test_unmapped_settlement_has_audited_investigation_without_fictitious_payment(harness):
    factory, user, _, _, _, _ = harness
    original = movement("pay_unknown")
    entity = dict(
        entity_id=original.provider_entity_id,
        type=original.movement_type,
        settlement_id=original.provider_settlement_id,
        payment_id=None,
        dispute_id=None,
        amount=original.amount_minor,
        currency=original.currency,
        debit=original.debit_minor,
        credit=original.credit_minor,
        fee=original.fee_minor,
        tax=original.tax_minor,
        settled=original.settled,
        on_hold=original.on_hold,
        created_at=int(original.provider_created_at.timestamp()),
        settled_at=int(original.provider_settled_at.timestamp()),
    )

    async def transport(request):
        assert request.method == "GET" and request.url.path.endswith("/settlements/recon")
        return httpx.Response(200, json=dict(entity="collection", count=1, items=[entity]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        evidence = await record_settlement(
            factory, provider.account_key, original, fee_includes_tax=True
        )
        command = new_uuid7()
        arguments = dict(
            actor=user.user_id,
            command_id=command,
            target_id=evidence.settlement_evidence_id,
            action="INVESTIGATE_SETTLEMENT",
            policy=POLICY,
            report_day=original.provider_created_at.date(),
            fee_includes_tax=True,
        )
        audit = await recover_financial_record(factory, provider, **arguments)
        replay = await recover_financial_record(factory, provider, **arguments)
    assert audit.result_code == "PROVIDER_BLOCKED" and audit.payment_id is None
    assert audit.settlement_evidence_id and replay.financial_audit_id == audit.financial_audit_id
    assert await counts(factory, CapturedCharge) == 0 and await counts(factory, Refund) == 0
    async with factory() as session:
        queue = await audit_inventory(
            None, session, settlement_evidence_id=audit.settlement_evidence_id
        )
        assert queue["audits"][0]["audit_id"] == audit.financial_audit_id
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(
            migration_command.downgrade, Config("alembic.ini"), "0021_financial_correctness"
        )
    async with factory() as session:
        assert (
            await session.get(FinancialAudit, audit.financial_audit_id)
        ).settlement_evidence_id == audit.settlement_evidence_id


async def test_fresh_truth_can_release_a_refund_hold_with_immutable_audit(harness):
    factory, user, _, _, provider, gateway = harness
    refund = await extra_refund(harness)
    await process(
        factory, provider, refund_body(refund, payment_id="pay_extra", amount=499), "evt_bad_amount"
    )
    gateway.refunds["rfnd_verified"] = dict(
        entity="refund",
        id="rfnd_verified",
        payment_id="pay_extra",
        amount=500,
        currency="INR",
        status="pending",
        receipt=f"rf_{refund.refund_id.hex}",
        notes={"tirodhan_refund_id": str(refund.refund_id)},
    )
    async with factory() as session:
        case = await session.scalar(
            select(FinancialException).where(
                FinancialException.reason_code == "REFUND_AMOUNT_MISMATCH"
            )
        )
    command = new_uuid7()

    async def invoke():
        return await recover_financial_record(
            factory,
            provider,
            actor=user.user_id,
            command_id=command,
            target_id=case.financial_exception_id,
            action="RESOLVE_EXCEPTION",
            policy=POLICY,
        )

    results = await asyncio.wait_for(asyncio.gather(invoke(), invoke()), 15)
    assert results[0].financial_audit_id == results[1].financial_audit_id
    assert results[0].result_code == "VERIFIED_HOLD_RELEASED"
    async with factory() as session:
        assert (
            await session.get(FinancialException, case.financial_exception_id)
        ).status == "RESOLVED"
        assert not (await session.get(RefundObligation, refund.refund_obligation_id)).payout_blocked
        current = await session.get(Refund, refund.refund_id)
        assert current.status == "SUBMITTED" and current.provider_refund_id == "rfnd_verified"
    assert not any(method == "POST" and path.endswith("/refund") for method, path in gateway.calls)


@pytest.mark.parametrize("change", ["contradiction", "role_revoked", "other_hold"])
async def test_hold_resolution_rechecks_changes_during_provider_get(harness, monkeypatch, change):
    from sqlalchemy import update

    from tirodhan.modules.identity.models import UserRole

    factory, user, _, _, provider, gateway = harness
    refund = await extra_refund(harness)
    await process(
        factory,
        provider,
        refund_body(refund, payment_id="pay_extra", amount=499),
        "evt_initial_hold",
    )
    gateway.refunds["rfnd_verified"] = dict(
        entity="refund",
        id="rfnd_verified",
        payment_id="pay_extra",
        amount=500,
        currency="INR",
        status="pending",
        receipt=f"rf_{refund.refund_id.hex}",
        notes={"tirodhan_refund_id": str(refund.refund_id)},
    )
    async with factory() as session:
        case = await session.scalar(
            select(FinancialException).where(
                FinancialException.reason_code == "REFUND_AMOUNT_MISMATCH"
            )
        )
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.inspect_charge_refunds

    async def pause(reference):
        entered.set()
        await release.wait()
        return await original(reference)

    monkeypatch.setattr(provider, "inspect_charge_refunds", pause)
    task = asyncio.create_task(
        recover_financial_record(
            factory,
            provider,
            actor=user.user_id,
            command_id=new_uuid7(),
            target_id=case.financial_exception_id,
            action="RESOLVE_EXCEPTION",
            policy=POLICY,
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    if change == "contradiction":
        await process(
            factory,
            provider,
            refund_body(refund, payment_id="pay_extra", id="rfnd_verified", amount=498),
            "evt_newer_hold",
        )
    elif change == "role_revoked":
        async with factory() as session, session.begin():
            await session.execute(
                update(UserRole)
                .where(UserRole.user_id == user.user_id, UserRole.role_code == "MANAGER")
                .values(revoked_at=utc_now(), revoked_by_user_id=user.user_id)
            )
    else:
        async with factory() as session, session.begin():
            payment = await session.get(Payment, refund.payment_id, with_for_update=True)
            charge = await session.get(CapturedCharge, refund.captured_charge_id)
            await open_exception(session, payment, "PAYMENT_DISPUTE", charge=charge)
    release.set()
    if change == "role_revoked":
        with pytest.raises(RefundConflictError):
            await asyncio.wait_for(task, 10)
    else:
        assert (await asyncio.wait_for(task, 10)).result_code == (
            "VERIFIED_CASE_RESOLVED_HELD" if change == "other_hold" else "PROVIDER_BLOCKED"
        )
    async with factory() as session:
        assert (await session.get(RefundObligation, refund.refund_obligation_id)).payout_blocked
        assert (await session.get(FinancialException, case.financial_exception_id)).status == (
            "RESOLVED" if change == "other_hold" else "OPEN"
        )
    assert not any(m == "POST" and p.endswith("/refund") for m, p in gateway.calls)


async def test_expired_previous_secret_and_tampered_body_are_rejected():
    from tirodhan.modules.payments.ports import PaymentProviderAuthenticationError

    body = b'{"event":"unhandled.event"}'
    headers = signed(body)
    headers["x-razorpay-signature"] = hmac.new(b"retiring-secret", body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient() as client:
        provider = RazorpayProvider(
            settings(
                razorpay_webhook_previous_secret="retiring-secret",
                razorpay_webhook_previous_secret_until=utc_now() - timedelta(seconds=1),
            ),
            client=client,
        )
        with pytest.raises(PaymentProviderAuthenticationError):
            await provider.authenticate_webhook(raw_body=body, headers=headers)
        await provider.authenticate_webhook(raw_body=body, headers=signed(body))
        with pytest.raises(PaymentProviderAuthenticationError):
            await provider.authenticate_webhook(raw_body=body + b" ", headers=signed(body))


async def test_corrected_settlement_releases_only_current_verified_hold(harness):
    factory, user, _, _, _, _ = harness
    await inquiry(harness)
    canonical = movement()
    entity = dict(
        entity_id=canonical.provider_entity_id,
        type="payment",
        settlement_id="setl_daily",
        payment_id=None,
        dispute_id=None,
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

    async def transport(request):
        assert request.method == "GET"
        items = [entity] if request.url.path.endswith("/settlements/recon") else []
        return httpx.Response(200, json=dict(entity="collection", count=len(items), items=items))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        await record_settlement(
            factory,
            provider.account_key,
            replace(canonical, credit_minor=469),
            fee_includes_tax=True,
        )
        async with factory() as session:
            case = await session.scalar(
                select(FinancialException).where(
                    FinancialException.reason_code == "SETTLEMENT_MISMATCH"
                )
            )
        audit = await recover_financial_record(
            factory,
            provider,
            actor=user.user_id,
            command_id=new_uuid7(),
            target_id=case.financial_exception_id,
            action="RESOLVE_EXCEPTION",
            policy=POLICY,
            report_day=canonical.provider_created_at.date(),
            fee_includes_tax=True,
        )
    assert audit.result_code == "VERIFIED_HOLD_RELEASED" and audit.settlement_evidence_id
    async with factory() as session:
        assert (
            await session.get(FinancialException, case.financial_exception_id)
        ).status == "RESOLVED"


@pytest.mark.parametrize("claimed", [False, True])
async def test_corrected_settlement_hold_preserves_unsubmitted_original_recovery(harness, claimed):
    factory, user, _, _, execution_provider, gateway = harness
    refund = await extra_refund(harness)
    if claimed:
        async with factory() as session, session.begin():
            current = await session.get(Refund, refund.refund_id)
            current.status = "PROCESSING"
            current.processing_started_at = utc_now()
    canonical = movement("pay_extra")
    entity = dict(
        entity_id="pay_extra",
        type="payment",
        settlement_id="setl_daily",
        payment_id=None,
        dispute_id=None,
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

    async def transport(request):
        assert request.method == "GET"
        items = [entity] if request.url.path.endswith("/settlements/recon") else []
        return httpx.Response(200, json=dict(entity="collection", count=len(items), items=items))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        await record_settlement(
            factory,
            provider.account_key,
            replace(canonical, credit_minor=469),
            fee_includes_tax=True,
        )
        async with factory() as session:
            case = await session.scalar(
                select(FinancialException).where(
                    FinancialException.reason_code == "SETTLEMENT_MISMATCH"
                )
            )
            current = await session.get(Refund, refund.refund_id)
            first_submission = current.processing_started_at
            original_key = current.provider_idempotency_key
            assert current.provider_refund_id is None
            assert (await session.get(RefundObligation, refund.refund_obligation_id)).payout_blocked
        audit = await recover_financial_record(
            factory,
            provider,
            actor=user.user_id,
            command_id=new_uuid7(),
            target_id=case.financial_exception_id,
            action="RESOLVE_EXCEPTION",
            policy=POLICY,
            report_day=canonical.provider_created_at.date(),
            fee_includes_tax=True,
        )
    assert audit.result_code == ("PROVIDER_BLOCKED" if claimed else "VERIFIED_HOLD_RELEASED")
    async with factory() as session:
        current = await session.get(Refund, refund.refund_id)
        assert current.processing_started_at == first_submission
        assert current.provider_idempotency_key == original_key
        assert current.status == ("PROCESSING" if claimed else "PENDING")
        assert current.provider_refund_id is None
        assert (
            await session.get(RefundObligation, refund.refund_obligation_id)
        ).payout_blocked is claimed
        queue = await inventory(manager=None, session=session)
        assert refund.payment_id in [r["payment_id"] for r in queue["payments"]]
    assert not gateway.refunds
    if claimed:
        with pytest.raises(RefundConflictError):
            await execute_refund_provider_call(factory, refund.refund_id, execution_provider)
        assert not gateway.refunds
    else:
        await execute_refund_provider_call(factory, refund.refund_id, execution_provider)
        assert len(gateway.refunds) == 1
        assert await counts(factory, Refund) == 1
        async with factory() as session:
            current = await session.get(Refund, refund.refund_id)
            assert current.status == "SUBMITTED" and current.completed_at is None
            assert current.processing_started_at is not None
            submitted_at = current.processing_started_at
            remote_id = current.provider_refund_id
            assert remote_id in gateway.refunds
        gateway.refunds[remote_id]["status"] = "processed"
        await inquire_target(
            factory, execution_provider, refund.refund_id, refund=True, policy=POLICY
        )
        await execute_refund_provider_call(factory, refund.refund_id, execution_provider)
        assert len(gateway.refunds) == 1
        assert (
            sum(method == "POST" and path.endswith("/refund") for method, path in gateway.calls)
            == 1
        )
        async with factory() as session:
            current = await session.get(Refund, refund.refund_id)
            assert current.status == "SUCCEEDED"
            assert current.completed_at is not None
            assert current.processing_started_at == submitted_at
            assert current.provider_refund_id == remote_id
            assert current.provider_idempotency_key == original_key
            assert (
                await session.get(FinancialException, case.financial_exception_id)
            ).status == "RESOLVED"


async def test_inventory_failure_is_visible_and_does_not_suppress_other_lane(harness):
    from tirodhan.modules.payments.models import FinancialScanCheckpoint, PaymentProviderEvent

    factory, _, _, _, _, _ = harness
    now = int(utc_now().timestamp())

    async def transport(request):
        if request.url.path.endswith("/payments"):
            return httpx.Response(429, json={"error": {"code": "RATE_LIMIT"}})
        item = dict(
            entity="refund",
            id="rfnd_unmapped",
            payment_id="pay_unmapped",
            amount=500,
            currency="INR",
            status="processed",
            receipt=None,
            notes={},
            created_at=now - 180,
        )
        return httpx.Response(200, json=dict(entity="collection", count=1, items=[item]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        policy = InventoryPolicy(now - 3600, 3600, 120, 60, 1)
        assert await discover_inventory(factory, provider, policy, POLICY, "PAYMENTS") == 0
        assert await discover_inventory(factory, provider, policy, POLICY, "REFUNDS") == 1
    async with factory() as session:
        failed = await session.scalar(
            select(FinancialScanCheckpoint).where(FinancialScanCheckpoint.scan_kind == "PAYMENTS")
        )
        assert failed.last_error_code == "PROVIDER_INVENTORY_UNCERTAIN" and failed.last_checked_at
        assert failed.page_offset == 0 and failed.last_exhausted_at is None
        assert await session.scalar(
            select(PaymentProviderEvent).where(
                PaymentProviderEvent.processing_status == "UNMATCHED"
            )
        )
    assert await counts(factory, Refund) == 0


@pytest.mark.parametrize("confirmed", [False, True])
async def test_replay_blocker_closes_only_on_proven_nonpayability_and_debt_stays_visible(
    harness, confirmed
):
    factory, _, _, _, provider, gateway = harness
    refund = await extra_refund(harness)
    provider._refund_finality = confirmed
    gateway.refunds["rfnd_original"] = dict(
        entity="refund",
        id="rfnd_original",
        payment_id="pay_extra",
        amount=500,
        currency="INR",
        status="failed",
        speed_requested="normal",
        receipt=f"rf_{refund.refund_id.hex}",
        notes={"tirodhan_refund_id": str(refund.refund_id)},
    )
    async with factory() as session, session.begin():
        payment = await session.get(Payment, refund.payment_id, with_for_update=True)
        case = await open_exception(
            session, payment, "REFUND_REPLAY_RETENTION_UNCONFIRMED", refund=refund
        )
    await execute_refund_provider_call(factory, refund.refund_id, provider)
    async with factory() as session:
        assert (await session.get(FinancialException, case.financial_exception_id)).status == (
            "RESOLVED" if confirmed else "OPEN"
        )
        assert (
            await session.get(RefundObligation, refund.refund_obligation_id)
        ).amount_minor == 500
        queue = await inventory(None, session)
        assert refund.payment_id in {item["payment_id"] for item in queue["payments"]}
    assert not any(m == "POST" and p.endswith("/refund") for m, p in gateway.calls)


@pytest.mark.parametrize("stage", ["approval", "execution"])
async def test_dashboard_refund_during_local_get_prevents_avoidable_post(
    harness, monkeypatch, stage
):
    factory, user, result, _, provider, gateway = harness
    if stage == "execution":
        refund = await extra_refund(harness)
        reference = "pay_extra"
    else:
        async with factory() as session, session.begin():
            request = await session.get(CollectionRequest, result.request.request_id)
            request.created_at = utc_now() - timedelta(hours=1)
            request.payment_expires_at = utc_now() - timedelta(seconds=1)
            await expire_pending_collection_requests(session, utc_now())
        await inquiry(harness)
        async with factory() as session:
            charge = await session.scalar(select(CapturedCharge))
        reference = charge.provider_payment_id
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.inspect_charge_refunds

    async def pause(payment_id):
        entered.set()
        await release.wait()
        return await original(payment_id)

    monkeypatch.setattr(provider, "inspect_charge_refunds", pause)
    task = asyncio.create_task(
        execute_refund_provider_call(factory, refund.refund_id, provider)
        if stage == "execution"
        else approve_refund(
            factory,
            provider,
            actor=user.user_id,
            command_id=new_uuid7(),
            charge_id=charge.captured_charge_id,
            policy=POLICY,
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    gateway.refunds["rfnd_dashboardRace"] = dict(
        entity="refund",
        id="rfnd_dashboardRace",
        payment_id=reference,
        amount=500,
        currency="INR",
        status="processed",
        receipt=None,
        notes={},
    )
    release.set()
    with pytest.raises(RefundConflictError):
        await asyncio.wait_for(task, 10)
    assert len(gateway.refunds) == 1
    assert not any(m == "POST" and p.endswith("/refund") for m, p in gateway.calls)
    async with factory() as session:
        queue = await inventory(None, session)
        assert queue["payments"]
        if stage == "execution":
            assert (await session.get(RefundObligation, refund.refund_obligation_id)).payout_blocked
            assert (await session.get(Refund, refund.refund_id)).provider_refund_id is None
        else:
            assert await counts(factory, Refund) == 0


@pytest.mark.parametrize("same_account", [False, True])
async def test_legacy_null_charge_is_bound_to_merchant_during_key_rotation(
    database_session_factory, same_account
):
    factory = database_session_factory
    refund = await legacy_intent(factory)
    remote = Refunds()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        old = RazorpayProvider(
            settings(razorpay_account_id="acc_original").model_copy(
                update={"razorpay_key_id": "rzp_test_old"}
            ),
            client=client,
        )
        rotated = RazorpayProvider(
            settings(
                razorpay_account_id="acc_original" if same_account else "acc_other",
            ).model_copy(update={"razorpay_key_id": "rzp_test_new"}),
            client=client,
        )
        async with factory() as session, session.begin():
            attempt = await session.get(PaymentAttempt, refund.payment_attempt_id)
            attempt.provider_account_key = old.account_key
        if same_account:
            await execute_refund_provider_call(factory, refund.refund_id, rotated)
            assert len(remote.effects) == 1
        else:
            from tirodhan.modules.payments.ports import PaymentProviderNotConfiguredError

            with pytest.raises(PaymentProviderNotConfiguredError):
                await execute_refund_provider_call(factory, refund.refund_id, rotated)
            assert not remote.effects and not remote.calls
            raw = json.loads(refund_body(refund))
            raw["account_id"] = "acc_other"
            event = await process(factory, rotated, json.dumps(raw).encode(), "evt_wrong_merchant")
            assert event.processing_status == "RECONCILIATION_REQUIRED"
            async with factory() as session:
                assert (await session.get(Refund, refund.refund_id)).status == "PENDING"
                assert await session.scalar(
                    select(FinancialException).where(
                        FinancialException.refund_id == refund.refund_id,
                        FinancialException.reason_code == "PROVIDER_ACCOUNT_MISMATCH",
                    )
                )


@pytest.mark.parametrize("state", ["reversed", "unrecognized"])
async def test_undocumented_provider_refund_state_preserves_debt_and_blocks_post(harness, state):
    factory, _, _, _, provider, gateway = harness
    refund = await extra_refund(harness)
    gateway.refunds["rfnd_original"] = dict(
        entity="refund",
        id="rfnd_original",
        payment_id="pay_extra",
        amount=500,
        currency="INR",
        status=state,
        receipt=f"rf_{refund.refund_id.hex}",
        notes={"tirodhan_refund_id": str(refund.refund_id)},
    )
    with pytest.raises(RefundConflictError):
        await execute_refund_provider_call(factory, refund.refund_id, provider)
    assert not any(m == "POST" and p.endswith("/refund") for m, p in gateway.calls)
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "INITIATION_UNCERTAIN"
        assert (
            await session.get(RefundObligation, refund.refund_obligation_id)
        ).amount_minor == 500


async def test_expected_membership_change_during_get_prevents_stale_hold_release(harness):
    factory, user, _, _, _, _ = harness
    await inquiry(harness)
    canonical = movement()
    entered, release = asyncio.Event(), asyncio.Event()
    entity = dict(
        entity_id=canonical.provider_entity_id,
        type="payment",
        settlement_id="setl_daily",
        payment_id=None,
        dispute_id=None,
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

    async def transport(request):
        assert request.method == "GET"
        if request.url.path.endswith("/refunds"):
            entered.set()
            await release.wait()
            items = []
        else:
            items = [entity]
        return httpx.Response(200, json=dict(entity="collection", count=len(items), items=items))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        async with factory() as session:
            charge = await session.scalar(select(CapturedCharge))
        expected = ExpectedSettlement(
            charge.captured_charge_id, None, "setl_daily", utc_now() - timedelta(days=1)
        )
        assert (
            await check_expected_settlement(
                factory, provider.account_key, [expected], coverage_verified=True
            )
            == 1
        )
        async with factory() as session:
            case = await session.scalar(
                select(FinancialException).where(
                    FinancialException.reason_code == "SETTLEMENT_EXPECTED_MISSING"
                )
            )
        task = asyncio.create_task(
            recover_financial_record(
                factory,
                provider,
                actor=user.user_id,
                command_id=new_uuid7(),
                target_id=case.financial_exception_id,
                action="RESOLVE_EXCEPTION",
                policy=POLICY,
                report_day=canonical.provider_created_at.date(),
                fee_includes_tax=True,
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        newer = replace(expected, settlement_id="setl_next")
        assert (
            await check_expected_settlement(
                factory, provider.account_key, [newer], coverage_verified=True
            )
            == 1
        )
        release.set()
        assert (await asyncio.wait_for(task, 10)).result_code == "PROVIDER_BLOCKED"
    async with factory() as session:
        current = await session.get(FinancialException, case.financial_exception_id)
        assert current.status == "OPEN" and current.expected_settlement_id == "setl_next"
