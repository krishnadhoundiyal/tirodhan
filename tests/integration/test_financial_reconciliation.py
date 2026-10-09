"""Financial truths, independently refundable charges and audited recovery on PostgreSQL."""

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from alembic import command as migration_command
from alembic.config import Config
from razorpay_helpers import Orders, booking, initiate, payment_body, process, settings
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from test_customer_mobile_core import app_for
from test_operational_api import _token_for

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.cancellation import cancel_collection_request_by_customer
from tirodhan.modules.collection_requests.expiry import expire_pending_collection_requests
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customer_reads.finance import load_financials
from tirodhan.modules.customer_reads.projections import payment_projection
from tirodhan.modules.identity.models import UserRole
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
    ReconciliationPolicy,
    approve_refund,
    inquire_target,
    manager_inquiry,
    reconcile_batch,
)
from tirodhan.modules.payments.refunds import RefundConflictError, execute_refund_provider_call
from tirodhan.modules.payments.service import PaymentNotEligibleError
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
POLICY = ReconciliationPolicy(10, 60, 3600, 120, 600, 86400, 30)


class Gateway(Orders):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.payments = []
        self.refunds = {}
        self.calls = []
        self.refund_timeout = False
        self.verify_no_connections = False

    def __call__(self, request):
        if self.verify_no_connections:
            assert self.engine.pool.checkedout() == 0
        self.calls.append((request.method, request.url.path))
        path = request.url.path.removeprefix("/v1")
        if path == "/orders":
            return super().__call__(request)
        if path.startswith("/orders/"):
            if path.endswith("/payments"):
                return httpx.Response(
                    200,
                    json=dict(entity="collection", count=len(self.payments), items=self.payments),
                )
            return httpx.Response(
                200, json=next(o for o in self.orders.values() if o["id"] == path.split("/")[-1])
            )
        if path.startswith("/refunds/"):
            item = self.refunds[path.split("/")[-1]]
            return httpx.Response(200, json=item)
        if path.endswith("/refund"):
            data = json.loads(request.content)
            ref = "rfnd_" + data["receipt"][3:]
            item = dict(
                entity="refund",
                id=ref,
                payment_id=path.split("/")[2],
                currency="INR",
                status="pending",
                speed_requested="normal",
                **data,
            )
            self.refunds[ref] = item
            if self.refund_timeout:
                raise httpx.ReadTimeout("synthetic ambiguous response", request=request)
            return httpx.Response(200, json=item)
        if path.endswith("/refunds"):
            items = [r for r in self.refunds.values() if r["payment_id"] == path.split("/")[2]]
            return httpx.Response(
                200, json=dict(entity="collection", count=len(items), items=items)
            )
        return httpx.Response(
            200, json=next(p for p in self.payments if p["id"] == path.split("/")[-1])
        )


@pytest_asyncio.fixture
async def harness(database_session_factory, database_engine):
    factory = database_session_factory
    user, result = await booking(factory)
    async with factory() as session, session.begin():
        session.add(UserRole(user_id=user.user_id, role_code="MANAGER", granted_at=utc_now()))
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        provider = RazorpayProvider(
            settings(razorpay_normal_refund_failure_finality_confirmed=True), client=client
        )
        attempt = await initiate(factory, user, result, provider)
        gateway.payments = [json.loads(payment_body(attempt))["payload"]["payment"]["entity"]]
        yield factory, user, result, attempt, provider, gateway


async def counts(factory, model):
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def inquiry(h, *, manager=False, command=None):
    factory, user, _, attempt, provider, _ = h
    if manager:
        return await manager_inquiry(
            factory,
            provider,
            command_id=command or new_uuid7(),
            actor=user.user_id,
            target_id=attempt.payment_attempt_id,
            refund=False,
            policy=POLICY,
        )
    return await inquire_target(
        factory, provider, attempt.payment_attempt_id, refund=False, policy=POLICY
    )


async def extra_refund(h):
    factory, _, _, _, _, gateway = h
    gateway.payments.append({**gateway.payments[0], "id": "pay_extra"})
    await inquiry(h)
    async with factory() as session:
        return await session.scalar(select(Refund))


async def test_webhook_poll_manager_converge_and_exact_manager_replay(harness):
    h = harness
    factory, _, result, attempt, provider, _ = h
    command = new_uuid7()
    await asyncio.wait_for(
        asyncio.gather(
            process(factory, provider, payment_body(attempt)),
            inquiry(h),
            inquiry(h, manager=True, command=command),
        ),
        15,
    )
    await inquiry(h, manager=True, command=command)
    assert await counts(factory, CapturedCharge) == 1
    assert await counts(factory, FinancialAudit) == 1
    async with factory() as session:
        assert (
            await session.get(CollectionRequest, result.request.request_id)
        ).status == "ACCEPTED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "CollectionRequestAccepted")
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_key.like("customer-financial:payment-confirmed:%"))
            )
            == 1
        )


async def test_five_minute_ui_timeout_is_not_financial_failure(harness):
    h = harness
    factory, _, result, attempt, _, _ = h
    async with factory() as session, session.begin():
        (await session.get(PaymentAttempt, attempt.payment_attempt_id)).created_at = (
            utc_now() - timedelta(minutes=6)
        )
    await inquiry(h)
    async with factory() as session:
        assert (
            await session.get(CollectionRequest, result.request.request_id)
        ).status == "ACCEPTED"


async def test_expiry_releases_booking_keeps_money_unresolved_then_manager_refunds(harness):
    h = harness
    factory, user, result, _, provider, _ = h
    async with factory() as session, session.begin():
        request = await session.get(CollectionRequest, result.request.request_id)
        request.created_at = utc_now() - timedelta(hours=1)
        request.payment_expires_at = utc_now() - timedelta(seconds=1)
    async with factory() as session, session.begin():
        assert await expire_pending_collection_requests(session, utc_now()) == 1
    async with factory() as session:
        request = await session.get(CollectionRequest, result.request.request_id)
        financial = (await load_financials(session, [request]))[request.request_id]
        assert financial.payment.status == "PENDING"
        assert (
            payment_projection(request, financial.payment, financial.attempts, now=utc_now()).status
            == "CONFIRMING"
        )
    await inquiry(h)
    assert await counts(factory, Refund) == 0
    async with factory() as session:
        charge = await session.scalar(select(CapturedCharge))
        assert (await session.get(Payment, charge.payment_id)).status == "SUCCEEDED"
        assert (await session.get(CollectionRequest, result.request.request_id)).status == "EXPIRED"
    audit = await approve_refund(
        factory,
        provider,
        actor=user.user_id,
        command_id=new_uuid7(),
        charge_id=charge.captured_charge_id,
        policy=POLICY,
    )
    assert audit.action == "HISTORICAL_REFUND"
    assert await counts(factory, Refund) == 1


@pytest.mark.parametrize(
    "state,allowed", [("failed", True), ("authorized", False), ("created", False)]
)
async def test_definitive_failure_permits_retry_uncertainty_blocks(harness, state, allowed):
    h = harness
    factory, user, result, _, provider, gateway = h
    gateway.payments[0].update(status=state, captured=False)
    await inquiry(h)
    if allowed:
        retry = await initiate(factory, user, result, provider, key="new-attempt")
        assert retry.status == "PENDING"
    else:
        with pytest.raises(PaymentNotEligibleError):
            await initiate(factory, user, result, provider, key="new-attempt")


async def test_failed_instrument_does_not_close_unresolved_order(harness):
    h = harness
    _, _, _, _, _, gateway = h
    gateway.payments = [
        {**gateway.payments[0], "status": "failed", "captured": False},
        {**gateway.payments[0], "id": "pay_unresolved", "status": "authorized", "captured": False},
    ]
    await inquiry(h)
    async with h[0]() as session:
        assert (await session.get(PaymentAttempt, h[3].payment_attempt_id)).status == "PENDING"


async def test_additional_charge_duplicate_events_and_multi_charge_cancellation(harness):
    h = harness
    factory, user, result, attempt, provider, _ = h
    extra = await extra_refund(h)
    await asyncio.gather(
        process(factory, provider, payment_body(attempt, payment_id="pay_extra"), "extra-repeat"),
        inquiry(h),
    )
    assert await counts(factory, CapturedCharge) == 2
    assert await counts(factory, Refund) == 1
    async with factory() as session, session.begin():
        await cancel_collection_request_by_customer(
            session,
            request_id=result.request.request_id,
            customer_id=user.user_id,
            idempotency_key="cancel",
            planning_lead_time_minutes=30,
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )
    async with factory() as session:
        refunds = list(await session.scalars(select(Refund)))
        assert len(refunds) == 2 and sum(r.amount_minor for r in refunds) == 1000
        assert len({r.captured_charge_id for r in refunds}) == 2
        snapshot = (
            await load_financials(
                session, [await session.get(CollectionRequest, result.request.request_id)]
            )
        )[result.request.request_id]
        assert len(snapshot.refunds) == 2
    await execute_refund_provider_call(factory, extra.refund_id, provider)
    assert h[5].refunds[next(iter(h[5].refunds))]["payment_id"] == "pay_extra"


@pytest.mark.parametrize("timeout", [False, True])
async def test_refund_missing_webhook_and_uncertain_initiation_reconciles_same_operation(
    harness, timeout
):
    h = harness
    factory, _, _, _, provider, gateway = h
    refund = await extra_refund(h)
    gateway.refund_timeout = timeout
    await execute_refund_provider_call(factory, refund.refund_id, provider)
    async with factory() as session:
        current = await session.get(Refund, refund.refund_id)
        assert current.status == ("INITIATION_UNCERTAIN" if timeout else "SUBMITTED")
    next(iter(gateway.refunds.values()))["status"] = "processed"
    await inquire_target(factory, provider, refund.refund_id, refund=True, policy=POLICY)
    await inquire_target(factory, provider, refund.refund_id, refund=True, policy=POLICY)
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "SUCCEEDED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.event_key
                    == f"customer-financial:refund-completed:{refund.refund_id}"
                )
            )
            == 1
        )
    assert await counts(factory, Refund) == 1


@pytest.mark.parametrize("state", ["pending", "processed"])
async def test_replacement_before_verified_non_payable_failure_rejected(harness, state):
    h = harness
    factory, user, _, _, provider, gateway = h
    refund = await extra_refund(h)
    await execute_refund_provider_call(factory, refund.refund_id, provider)
    next(iter(gateway.refunds.values()))["status"] = state
    with pytest.raises(RefundConflictError):
        await approve_refund(
            factory,
            provider,
            actor=user.user_id,
            command_id=new_uuid7(),
            charge_id=refund.captured_charge_id,
            failed_refund_id=refund.refund_id,
            policy=POLICY,
        )
    assert await counts(factory, Refund) == 1


async def test_verified_failed_refund_concurrent_managers_one_replacement_and_late_success_blocks(
    harness,
):
    h = harness
    factory, user, _, _, provider, gateway = h
    original = await extra_refund(h)
    await execute_refund_provider_call(factory, original.refund_id, provider)
    next(iter(gateway.refunds.values()))["status"] = "failed"

    async def approve():
        try:
            return await approve_refund(
                factory,
                provider,
                actor=user.user_id,
                command_id=new_uuid7(),
                charge_id=original.captured_charge_id,
                failed_refund_id=original.refund_id,
                policy=POLICY,
            )
        except RefundConflictError:
            return None

    results = await asyncio.wait_for(asyncio.gather(approve(), approve()), 15)
    assert sum(r is not None for r in results) == 1
    assert await counts(factory, Refund) == 2
    assert await counts(factory, RefundObligation) == 1
    next(iter(gateway.refunds.values()))["status"] = "processed"
    await inquire_target(factory, provider, original.refund_id, refund=True, policy=POLICY)
    async with factory() as session:
        obligation = await session.get(RefundObligation, original.refund_obligation_id)
        assert obligation.payout_blocked
        replacement = await session.scalar(
            select(Refund).where(Refund.refund_id != original.refund_id)
        )
    with pytest.raises(RefundConflictError):
        await execute_refund_provider_call(factory, replacement.refund_id, provider)


async def test_historical_cancelled_no_implicit_payout_manager_approval_replays(harness):
    h = harness
    factory, user, result, _, provider, _ = h
    async with factory() as session, session.begin():
        request = await session.get(CollectionRequest, result.request.request_id)
        request.status = "CANCELLED"
        request.cancelled_at = utc_now()
    await inquiry(h, manager=True)
    assert await counts(factory, Refund) == 0
    async with factory() as session:
        charge = await session.scalar(select(CapturedCharge))
        assert await session.scalar(
            select(FinancialException.financial_exception_id).where(
                FinancialException.reason_code == "HISTORICAL_CANCELLED_MISSING_REFUND"
            )
        )
    command = new_uuid7()
    audit = await approve_refund(
        factory,
        provider,
        actor=user.user_id,
        command_id=command,
        charge_id=charge.captured_charge_id,
        policy=POLICY,
    )
    replay = await approve_refund(
        factory,
        provider,
        actor=user.user_id,
        command_id=command,
        charge_id=charge.captured_charge_id,
        policy=POLICY,
    )
    assert replay.financial_audit_id == audit.financial_audit_id
    assert await counts(factory, Refund) == 1


async def test_overlapping_jobs_claim_once_and_missing_order_remains_unresolved(harness):
    h = harness
    factory, _, _, attempt, provider, gateway = h
    gateway.payments = []
    counts_run = await asyncio.gather(
        reconcile_batch(factory, provider, POLICY), reconcile_batch(factory, provider, POLICY)
    )
    assert sum(counts_run) == 1
    async with factory() as session:
        current = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        assert (
            current.status == "PENDING"
            and current.check_count == 1
            and current.next_check_at > utc_now()
        )


async def test_wrong_reported_payment_reference_does_not_supply_success_evidence(harness):
    h = harness
    factory, user, _, attempt, provider, gateway = h
    gateway.payments[0]["order_id"] = "order_wrong"
    audit = await manager_inquiry(
        factory,
        provider,
        command_id=new_uuid7(),
        actor=user.user_id,
        target_id=attempt.payment_attempt_id,
        refund=False,
        policy=POLICY,
        reported_reference="pay_success",
    )
    assert audit.result_code == "UNRESOLVED" and audit.evidence_id is None
    assert await counts(factory, CapturedCharge) == 0
    assert await counts(factory, FinancialAudit) == 1


async def test_failed_status_without_confirmed_finality_never_releases_obligation(harness):
    h = harness
    factory, user, _, _, configured, gateway = h
    refund = await extra_refund(h)
    await execute_refund_provider_call(factory, refund.refund_id, configured)
    next(iter(gateway.refunds.values()))["status"] = "failed"
    provider = RazorpayProvider(settings(), client=configured._client)
    command = new_uuid7()
    for _ in range(2):
        with pytest.raises(RefundConflictError):
            await approve_refund(
                factory,
                provider,
                actor=user.user_id,
                command_id=command,
                charge_id=refund.captured_charge_id,
                failed_refund_id=refund.refund_id,
                policy=POLICY,
            )
    async with factory() as session:
        current = await session.get(Refund, refund.refund_id)
        assert current.status == "FAILED" and current.non_payable_verified_at is None
        obligation = await session.get(RefundObligation, refund.refund_obligation_id)
        assert obligation.amount_minor == 500
        audit = await session.scalar(select(FinancialAudit))
        assert audit.result_code == "REFUND_APPROVAL_REFUSED"
    assert await counts(factory, FinancialAudit) == 1
    assert await counts(factory, Refund) == 1
    # Later authoritative success settles this obligation and closes the failure case;
    # its refused approval/audit and failed evidence remain historical facts.
    next(iter(gateway.refunds.values()))["status"] = "processed"
    await inquire_target(factory, provider, refund.refund_id, refund=True, policy=POLICY)
    async with factory() as session:
        current = await session.get(Refund, refund.refund_id)
        assert current.status == "SUCCEEDED"
        case = await session.scalar(
            select(FinancialException).where(
                FinancialException.refund_id == refund.refund_id,
                FinancialException.reason_code == "REFUND_FAILED",
            )
        )
        assert case.status == "RESOLVED"


async def test_database_enforces_per_charge_reservations(harness):
    h = harness
    factory = h[0]
    original = await extra_refund(h)
    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            duplicate = Refund(
                payment_id=original.payment_id,
                payment_attempt_id=original.payment_attempt_id,
                captured_charge_id=original.captured_charge_id,
                refund_obligation_id=original.refund_obligation_id,
                amount_minor=1,
                currency="INR",
                reason_code="ADDITIONAL_SUCCESS",
                status="PENDING",
                provider="RAZORPAY",
                provider_idempotency_key=f"refund:{new_uuid7()}",
            )
            session.add(duplicate)
            await session.flush()
    assert await counts(factory, Refund) == 1


async def test_provider_io_has_no_held_database_connection(harness):
    h = harness
    factory, user, _, _, provider, gateway = h
    gateway.verify_no_connections = True
    refund = await extra_refund(h)
    await execute_refund_provider_call(factory, refund.refund_id, provider)
    next(iter(gateway.refunds.values()))["status"] = "failed"
    await approve_refund(
        factory,
        provider,
        actor=user.user_id,
        command_id=new_uuid7(),
        charge_id=refund.captured_charge_id,
        failed_refund_id=refund.refund_id,
        policy=POLICY,
    )


async def test_manager_http_live_rbac_and_inventory(harness):
    h = harness
    factory, user, result, attempt, provider, _ = h
    token, _ = await _token_for(factory, user.user_id)
    app = app_for(factory, None)
    app.state.payment_provider = provider
    app.state.settings = settings(
        command_idempotency_ttl_seconds=86400,
        planning_lead_time_minutes=30,
        financial_reconciliation_batch_size=10,
        financial_reconciliation_interval_seconds=60,
        financial_reconciliation_max_backoff_seconds=3600,
        financial_reconciliation_lease_seconds=120,
        financial_unresolved_threshold_seconds=600,
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/manager/financial/payments")).status_code == 401
        response = await client.get("/v1/manager/financial/payments", headers=headers)
        assert response.status_code == 200 and len(response.json()["payments"]) == 1
        command = {"command_id": str(new_uuid7()), "reported_payment_id": "pay_success"}
        response = await client.post(
            f"/v1/manager/financial/payment-attempts/{attempt.payment_attempt_id}/reconcile",
            json=command,
            headers=headers,
        )
        assert response.status_code == 200 and response.json()["result_code"] == "PROCESSED"
        response = await client.get(
            f"/v1/manager/financial/payments/{result.payment.payment_id}", headers=headers
        )
        assert response.status_code == 200 and len(response.json()["charges"]) == 1
        async with factory() as session, session.begin():
            role = await session.scalar(
                select(UserRole).where(
                    UserRole.user_id == user.user_id, UserRole.role_code == "MANAGER"
                )
            )
            role.revoked_at = utc_now()
        assert (
            await client.get("/v1/manager/financial/payments", headers=headers)
        ).status_code == 403
        assert (
            await client.post(
                f"/v1/manager/financial/payment-attempts/{attempt.payment_attempt_id}/reconcile",
                json=command,
                headers=headers,
            )
        ).status_code == 403


async def test_scheduled_refund_inquiry_completes_without_webhook(harness):
    h = harness
    factory, _, _, _, provider, gateway = h
    refund = await extra_refund(h)
    await execute_refund_provider_call(factory, refund.refund_id, provider)
    next(iter(gateway.refunds.values()))["status"] = "processed"
    assert await reconcile_batch(factory, provider, POLICY) == 1
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).status == "SUCCEEDED"


async def test_historical_external_refund_blocks_duplicate_compensation(harness):
    h = harness
    factory, user, result, _, provider, gateway = h
    async with factory() as session, session.begin():
        (await session.get(CollectionRequest, result.request.request_id)).status = "CANCELLED"
    await inquiry(h, manager=True)
    gateway.refunds["rfnd_external"] = dict(
        id="rfnd_external",
        entity="refund",
        payment_id="pay_success",
        currency="INR",
        amount=500,
        status="pending",
        receipt=None,
    )
    async with factory() as session:
        charge = await session.scalar(select(CapturedCharge))
    with pytest.raises(RefundConflictError):
        await approve_refund(
            factory,
            provider,
            actor=user.user_id,
            command_id=new_uuid7(),
            charge_id=charge.captured_charge_id,
            policy=POLICY,
        )
    assert await counts(factory, Refund) == 0


async def test_manager_crash_after_financial_commit_replays_one_business_effect(
    harness, monkeypatch
):
    from tirodhan.modules.payments import reconciliation

    h = harness
    factory = h[0]
    command = new_uuid7()
    original = reconciliation.inquire_target

    async def crash_after_committed_evidence(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated crash before audit commit")

    with monkeypatch.context() as patch:
        patch.setattr(reconciliation, "inquire_target", crash_after_committed_evidence)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await inquiry(h, manager=True, command=command)
    assert await counts(factory, CapturedCharge) == 1
    assert await counts(factory, FinancialAudit) == 0
    await inquiry(h, manager=True, command=command)
    assert await counts(factory, FinancialAudit) == 1
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "CollectionRequestAccepted")
            )
            == 1
        )


async def test_expired_reconciliation_claim_is_recovered_without_waiting_for_memory(harness):
    h = harness
    factory, _, _, attempt, provider, _ = h
    async with factory() as session, session.begin():
        current = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        current.claim_token = new_uuid7()
        current.claim_until = utc_now() - timedelta(seconds=1)
        current.next_check_at = current.claim_until
    assert await reconcile_batch(factory, provider, POLICY) == 1
    assert await counts(factory, CapturedCharge) == 1
    async with factory() as session:
        current = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        assert current.claim_token is None and current.next_check_at is None


async def test_manager_command_cannot_replay_with_a_different_reference(harness):
    h = harness
    factory, user, _, attempt, provider, _ = h
    command = new_uuid7()
    await inquiry(h, manager=True, command=command)
    with pytest.raises(IdempotencyKeyConflictError):
        await manager_inquiry(
            factory,
            provider,
            actor=user.user_id,
            command_id=command,
            target_id=attempt.payment_attempt_id,
            refund=False,
            policy=POLICY,
            reported_reference="pay_different",
        )
    assert await counts(factory, FinancialAudit) == 1


async def test_customer_events_contain_only_identifiers_and_committed_changes(harness):
    h = harness
    factory = h[0]
    await extra_refund(h)
    await inquiry(h)
    async with factory() as session:
        events = list(
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "CustomerFinancialStatusChanged"
                )
            )
        )
        assert len(events) == 2
        assert all(set(event.payload) == {"request_id", "change"} for event in events)
        assert {event.payload["change"] for event in events} == {
            "PAYMENT_CONFIRMED",
            "REFUND_INITIATED",
        }


async def test_reconciliation_migration_roundtrip_preserves_populated_original_history(harness):
    h = harness
    factory = h[0]
    refund = await extra_refund(h)
    tables = ("payment", "payment_attempt", "refund", "payment_provider_event")

    async def original_counts():
        async with factory() as session:
            return {
                table: await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in tables
            }

    before = await original_counts()
    assert all(before.values())
    try:
        await asyncio.to_thread(
            migration_command.downgrade, Config("alembic.ini"), "0019_customer_financial_events"
        )
        assert await original_counts() == before
        async with factory() as session:
            assert (
                await session.scalar(
                    text("SELECT amount_minor FROM refund WHERE refund_id=:id"),
                    {"id": refund.refund_id},
                )
                == 500
            )
    finally:
        await asyncio.to_thread(migration_command.upgrade, Config("alembic.ini"), "head")
    assert await original_counts() == before
    async with factory() as session:
        assert (await session.get(Refund, refund.refund_id)).amount_minor == 500
        assert await session.scalar(select(func.count()).select_from(CapturedCharge)) == 0


async def test_terminal_payment_closes_uncertainty_case_without_closing_commercial_review(harness):
    h = harness
    factory, _, _, attempt, provider, gateway = h
    gateway.payments[0].update(status="authorized", captured=False)
    async with factory() as session, session.begin():
        current = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        current.created_at = utc_now() - timedelta(minutes=11)
    await reconcile_batch(factory, provider, POLICY)
    async with factory() as session, session.begin():
        case = await session.scalar(select(FinancialException))
        assert case.status == "OPEN" and case.reason_code == "PAYMENT_UNRESOLVED"
        (await session.get(PaymentAttempt, attempt.payment_attempt_id)).next_check_at = utc_now()
    gateway.payments[0].update(status="captured", captured=True)
    await reconcile_batch(factory, provider, POLICY)
    async with factory() as session:
        case = await session.scalar(select(FinancialException))
        assert case.status == "RESOLVED" and case.resolved_at is not None


async def test_pending_after_verified_failure_blocks_already_approved_replacement(harness):
    h = harness
    factory, user, _, _, provider, gateway = h
    original = await extra_refund(h)
    await execute_refund_provider_call(factory, original.refund_id, provider)
    remote = next(iter(gateway.refunds.values()))
    remote["status"] = "failed"
    audit = await approve_refund(
        factory,
        provider,
        actor=user.user_id,
        command_id=new_uuid7(),
        charge_id=original.captured_charge_id,
        failed_refund_id=original.refund_id,
        policy=POLICY,
    )
    remote["status"] = "pending"
    await inquire_target(factory, provider, original.refund_id, refund=True, policy=POLICY)
    before = len([call for call in gateway.calls if call[0] == "POST"])
    with pytest.raises(RefundConflictError, match="blocked"):
        await execute_refund_provider_call(factory, audit.refund_id, provider)
    assert len([call for call in gateway.calls if call[0] == "POST"]) == before
    async with factory() as session:
        obligation = await session.get(RefundObligation, original.refund_obligation_id)
        assert obligation.payout_blocked
        current = await session.get(Refund, original.refund_id)
        assert current.non_payable_verified_at is None
        assert (await session.get(Refund, audit.refund_id)).status == "PENDING"
