from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.service import (
    PAYMENT_PENDING,
    REQUEST_ACCEPTED,
    REQUEST_PENDING_PAYMENT,
    REQUEST_PRE_PLANNING,
)
from tirodhan.modules.identity.models import AppUser
from tirodhan.modules.payments.models import Payment, PaymentAttempt
from tirodhan.modules.payments.ports import AuthenticatedPaymentEvent, PaymentEventOutcome
from tirodhan.modules.payments.service import (
    ATTEMPT_PENDING,
    ATTEMPT_SUCCEEDED,
    EVENT_PROCESSED,
    EVENT_RECONCILIATION,
    process_authenticated_payment_event,
)
from tirodhan.modules.planning.locking import acquire_work_unit_advisory_lock
from tirodhan.modules.planning.models import PlanningBatch, PlanningBatchAttempt
from tirodhan.modules.planning.service import (
    PLANNING_ATTEMPT_STARTED,
    PLANNING_BATCH_READY,
    PLANNING_BATCH_READY_MESSAGE,
    PlanningMessage,
    PlanningWorkUnit,
    discover_due_planning_work_units,
    freeze_planning_batch,
    prepare_planning_attempt,
)
from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent
from tirodhan.modules.reliability.primitives import INBOX_PROCESSING
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.modules.serviceability.service import SERVICEABILITY_SERVICEABLE

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LEAD_TIME_MINUTES = 30
MAX_ATTEMPTS = 3


async def create_user(factory: async_sessionmaker[AsyncSession]) -> AppUser:
    async with factory() as session, session.begin():
        user = AppUser(status="ACTIVE")
        session.add(user)
        await session.flush([user])
        return user


async def create_request_record(
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    *,
    cell_id: str,
    slot_start: datetime,
    slot_end: datetime,
    status: str,
    planning_batch_id: UUID | None = None,
    with_payment: bool = False,
) -> tuple[CollectionRequest, Payment | None, PaymentAttempt | None]:
    now = utc_now()
    async with factory() as session, session.begin():
        context = ServiceabilityContext(
            serviceability_context_id=new_uuid7(),
            user_id=user_id,
            address_snapshot_encrypted=b"test-envelope:planning-address",
            location=WKTElement("POINT(77.2090 28.6139)", srid=4326),
            cell_id=cell_id,
            status=SERVICEABILITY_SERVICEABLE,
            expires_at=now + timedelta(days=1),
            created_at=now,
            resolved_at=now,
        )
        request = CollectionRequest(
            request_id=new_uuid7(),
            client_request_id=new_uuid7(),
            customer_id=user_id,
            serviceability_context_id=context.serviceability_context_id,
            pickup_address_snapshot_encrypted=b"test-envelope:planning-address",
            pickup_location=WKTElement("POINT(77.2090 28.6139)", srid=4326),
            cell_id=cell_id,
            slot_start=slot_start,
            slot_end=slot_end,
            quoted_amount_minor=500,
            currency="INR",
            status=status,
            payment_expires_at=now + timedelta(hours=2),
            planning_batch_id=planning_batch_id,
            created_at=now,
            accepted_at=now if status == REQUEST_ACCEPTED else None,
        )
        session.add(context)
        await session.flush([context])
        session.add(request)
        await session.flush([request])
        if not with_payment:
            return request, None, None

        payment = Payment(
            payment_id=new_uuid7(),
            request_id=request.request_id,
            amount_minor=500,
            currency="INR",
            status=PAYMENT_PENDING,
            created_at=now,
        )
        attempt = PaymentAttempt(
            payment_attempt_id=new_uuid7(),
            payment_id=payment.payment_id,
            provider="testpay",
            provider_order_id=f"order-{request.request_id}",
            provider_idempotency_key=f"payment-attempt:{new_uuid7()}",
            status=ATTEMPT_PENDING,
            created_at=now,
        )
        session.add(payment)
        await session.flush([payment])
        session.add(attempt)
        await session.flush([attempt])
        return request, payment, attempt


def successful_event(attempt: PaymentAttempt, event_id: str) -> AuthenticatedPaymentEvent:
    return AuthenticatedPaymentEvent(
        provider=attempt.provider,
        external_event_id=event_id,
        event_type="payment.succeeded",
        outcome=PaymentEventOutcome.SUCCEEDED,
        payment_attempt_id=attempt.payment_attempt_id,
        provider_order_id=attempt.provider_order_id,
        provider_payment_id=f"payment-{event_id}",
    )


async def create_ready_batch(
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    *,
    cell_id: str,
    slot_start: datetime,
) -> PlanningBatch:
    request, _, _ = await create_request_record(
        factory,
        user_id,
        cell_id=cell_id,
        slot_start=slot_start,
        slot_end=slot_start + timedelta(minutes=30),
        status=REQUEST_ACCEPTED,
    )
    result = await freeze_planning_batch(
        factory,
        PlanningWorkUnit(
            cell_id=cell_id,
            slot_start=request.slot_start,
            slot_end=request.slot_end,
        ),
        lead_time_minutes=LEAD_TIME_MINUTES,
        max_attempts=MAX_ATTEMPTS,
        now=slot_start - timedelta(minutes=LEAD_TIME_MINUTES),
    )
    assert result.batch is not None
    return result.batch


@pytest.mark.asyncio
async def test_payment_success_respects_temporal_planning_cutoff_without_batch(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    now = utc_now()
    before_request, before_payment, before_attempt = await create_request_record(
        database_session_factory,
        user.user_id,
        cell_id="before-cutoff-cell",
        slot_start=now + timedelta(minutes=60),
        slot_end=now + timedelta(minutes=90),
        status=REQUEST_PENDING_PAYMENT,
        with_payment=True,
    )
    blocked_request, blocked_payment, blocked_attempt = await create_request_record(
        database_session_factory,
        user.user_id,
        cell_id="at-cutoff-cell",
        slot_start=now + timedelta(minutes=20),
        slot_end=now + timedelta(minutes=50),
        status=REQUEST_PENDING_PAYMENT,
        with_payment=True,
    )
    assert before_payment is not None and before_attempt is not None
    assert blocked_payment is not None and blocked_attempt is not None

    accepted_event = await process_authenticated_payment_event(
        database_session_factory,
        successful_event(before_attempt, "before-cutoff-success"),
        payload_hash=hashlib.sha256(b"before-cutoff").digest(),
        planning_lead_time_minutes=LEAD_TIME_MINUTES,
    )
    blocked_event = await process_authenticated_payment_event(
        database_session_factory,
        successful_event(blocked_attempt, "at-cutoff-success"),
        payload_hash=hashlib.sha256(b"at-cutoff").digest(),
        planning_lead_time_minutes=LEAD_TIME_MINUTES,
    )

    async with database_session_factory() as session:
        accepted = await session.get(CollectionRequest, before_request.request_id)
        blocked = await session.get(CollectionRequest, blocked_request.request_id)
        accepted_payment = await session.get(Payment, before_payment.payment_id)
        still_pending_payment = await session.get(Payment, blocked_payment.payment_id)
        blocked_truth = await session.get(PaymentAttempt, blocked_attempt.payment_attempt_id)
        acceptance_events = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.event_type == "CollectionRequestAccepted"
            )
        )

    assert accepted_event.processing_status == EVENT_PROCESSED
    assert accepted is not None and accepted.status == REQUEST_ACCEPTED
    assert accepted_payment is not None and accepted_payment.status == "SUCCEEDED"
    assert blocked_event.processing_status == EVENT_RECONCILIATION
    assert blocked_event.failure_code == "PLANNING_CUTOFF_REACHED"
    assert blocked is not None and blocked.status == REQUEST_PENDING_PAYMENT
    assert still_pending_payment is not None and still_pending_payment.status == PAYMENT_PENDING
    assert blocked_truth is not None and blocked_truth.status == ATTEMPT_SUCCEEDED
    assert acceptance_events == 1


@pytest.mark.asyncio
async def test_discovery_returns_only_distinct_due_accepted_work_units(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    now = utc_now().replace(microsecond=0)
    due_start = now + timedelta(minutes=20)
    due_end = due_start + timedelta(minutes=30)
    for _ in range(2):
        await create_request_record(
            database_session_factory,
            user.user_id,
            cell_id="due-cell",
            slot_start=due_start,
            slot_end=due_end,
            status=REQUEST_ACCEPTED,
        )
    await create_request_record(
        database_session_factory,
        user.user_id,
        cell_id="future-cell",
        slot_start=now + timedelta(minutes=31),
        slot_end=now + timedelta(minutes=61),
        status=REQUEST_ACCEPTED,
    )
    await create_request_record(
        database_session_factory,
        user.user_id,
        cell_id="pending-cell",
        slot_start=due_start,
        slot_end=due_end,
        status=REQUEST_PENDING_PAYMENT,
    )

    discovered = await discover_due_planning_work_units(
        database_session_factory,
        lead_time_minutes=LEAD_TIME_MINUTES,
        now=now,
    )

    assert discovered == (
        PlanningWorkUnit(cell_id="due-cell", slot_start=due_start, slot_end=due_end),
    )


@pytest.mark.asyncio
async def test_freeze_selects_exact_population_snapshots_config_and_is_idempotent(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    now = utc_now().replace(microsecond=0)
    slot_start = now + timedelta(minutes=20)
    slot_end = slot_start + timedelta(minutes=30)
    target_requests = [
        (
            await create_request_record(
                database_session_factory,
                user.user_id,
                cell_id="target-cell",
                slot_start=slot_start,
                slot_end=slot_end,
                status=REQUEST_ACCEPTED,
            )
        )[0]
        for _ in range(2)
    ]
    async with database_session_factory() as session, session.begin():
        unrelated_batch = PlanningBatch(
            planning_batch_id=new_uuid7(),
            cell_id="unrelated-batch-cell",
            slot_start=slot_start + timedelta(days=1),
            slot_end=slot_end + timedelta(days=1),
            status=PLANNING_BATCH_READY,
            max_attempts_snapshot=MAX_ATTEMPTS,
            created_at=now,
        )
        session.add(unrelated_batch)
        await session.flush([unrelated_batch])
    exclusions = [
        (
            await create_request_record(
                database_session_factory,
                user.user_id,
                cell_id="other-cell",
                slot_start=slot_start,
                slot_end=slot_end,
                status=REQUEST_ACCEPTED,
            )
        )[0],
        (
            await create_request_record(
                database_session_factory,
                user.user_id,
                cell_id="target-cell",
                slot_start=slot_start + timedelta(minutes=30),
                slot_end=slot_end + timedelta(minutes=30),
                status=REQUEST_ACCEPTED,
            )
        )[0],
        (
            await create_request_record(
                database_session_factory,
                user.user_id,
                cell_id="target-cell",
                slot_start=slot_start,
                slot_end=slot_end + timedelta(minutes=30),
                status=REQUEST_ACCEPTED,
            )
        )[0],
        (
            await create_request_record(
                database_session_factory,
                user.user_id,
                cell_id="target-cell",
                slot_start=slot_start,
                slot_end=slot_end,
                status=REQUEST_PENDING_PAYMENT,
            )
        )[0],
        (
            await create_request_record(
                database_session_factory,
                user.user_id,
                cell_id="target-cell",
                slot_start=slot_start,
                slot_end=slot_end,
                status=REQUEST_ACCEPTED,
                planning_batch_id=unrelated_batch.planning_batch_id,
            )
        )[0],
    ]
    work_unit = PlanningWorkUnit("target-cell", slot_start, slot_end)

    frozen = await freeze_planning_batch(
        database_session_factory,
        work_unit,
        lead_time_minutes=LEAD_TIME_MINUTES,
        max_attempts=MAX_ATTEMPTS,
        now=now,
    )
    replay = await freeze_planning_batch(
        database_session_factory,
        work_unit,
        lead_time_minutes=LEAD_TIME_MINUTES,
        max_attempts=9,
        now=now,
    )

    assert frozen.batch is not None
    async with database_session_factory() as session:
        targets = [
            await session.get(CollectionRequest, item.request_id) for item in target_requests
        ]
        excluded = [await session.get(CollectionRequest, item.request_id) for item in exclusions]
        outbox = list(
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == PLANNING_BATCH_READY_MESSAGE)
            )
        )
        persisted_batch = await session.get(PlanningBatch, frozen.batch.planning_batch_id)

    assert frozen.created is True
    assert frozen.transitioned_request_count == 2
    assert replay.created is False
    assert replay.batch is not None
    assert replay.batch.planning_batch_id == frozen.batch.planning_batch_id
    assert all(item is not None and item.status == REQUEST_PRE_PLANNING for item in targets)
    assert {item.planning_batch_id for item in targets if item is not None} == {
        frozen.batch.planning_batch_id
    }
    assert all(item is not None and item.status != REQUEST_PRE_PLANNING for item in excluded)
    assert excluded[-1] is not None
    assert excluded[-1].planning_batch_id == unrelated_batch.planning_batch_id
    assert persisted_batch is not None
    assert persisted_batch.status == PLANNING_BATCH_READY
    assert persisted_batch.max_attempts_snapshot == MAX_ATTEMPTS
    assert persisted_batch.algorithm_version is None
    assert persisted_batch.completion_mode is None
    assert persisted_batch.completed_at is None
    assert len(outbox) == 1
    assert outbox[0].payload == {
        "planning_batch_id": str(frozen.batch.planning_batch_id),
        "cell_id": "target-cell",
        "attempt_number": 1,
    }


@pytest.mark.asyncio
async def test_overlapping_freeze_creates_one_batch_and_zero_work_creates_none(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    now = utc_now().replace(microsecond=0)
    slot_start = now + timedelta(minutes=10)
    work_unit = PlanningWorkUnit("concurrent-cell", slot_start, slot_start + timedelta(minutes=30))
    await create_request_record(
        database_session_factory,
        user.user_id,
        cell_id=work_unit.cell_id,
        slot_start=work_unit.slot_start,
        slot_end=work_unit.slot_end,
        status=REQUEST_ACCEPTED,
    )

    results = await asyncio.gather(
        freeze_planning_batch(
            database_session_factory,
            work_unit,
            lead_time_minutes=LEAD_TIME_MINUTES,
            max_attempts=MAX_ATTEMPTS,
            now=now,
        ),
        freeze_planning_batch(
            database_session_factory,
            work_unit,
            lead_time_minutes=LEAD_TIME_MINUTES,
            max_attempts=MAX_ATTEMPTS,
            now=now,
        ),
    )
    empty_unit = PlanningWorkUnit(
        "empty-cell",
        slot_start,
        slot_start + timedelta(minutes=30),
    )
    no_work = await freeze_planning_batch(
        database_session_factory,
        empty_unit,
        lead_time_minutes=LEAD_TIME_MINUTES,
        max_attempts=MAX_ATTEMPTS,
        now=now,
    )

    async with database_session_factory() as session:
        batch_count = await session.scalar(select(func.count(PlanningBatch.planning_batch_id)))
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.event_type == PLANNING_BATCH_READY_MESSAGE
            )
        )

    assert sum(result.created for result in results) == 1
    batch_ids = {result.batch.planning_batch_id for result in results if result.batch is not None}
    assert len(batch_ids) == 1
    assert no_work.batch is None
    assert no_work.transitioned_request_count == 0
    assert batch_count == 1
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_payment_acceptance_then_freeze_includes_request_consistently(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    now = utc_now().replace(microsecond=0)
    slot_start = now + timedelta(minutes=60)
    request, _, attempt = await create_request_record(
        database_session_factory,
        user.user_id,
        cell_id="payment-first-cell",
        slot_start=slot_start,
        slot_end=slot_start + timedelta(minutes=30),
        status=REQUEST_PENDING_PAYMENT,
        with_payment=True,
    )
    assert attempt is not None
    accepted = await process_authenticated_payment_event(
        database_session_factory,
        successful_event(attempt, "payment-first-success"),
        payload_hash=hashlib.sha256(b"payment-first").digest(),
        planning_lead_time_minutes=LEAD_TIME_MINUTES,
    )
    frozen = await freeze_planning_batch(
        database_session_factory,
        PlanningWorkUnit(request.cell_id, request.slot_start, request.slot_end),
        lead_time_minutes=LEAD_TIME_MINUTES,
        max_attempts=MAX_ATTEMPTS,
        now=slot_start - timedelta(minutes=LEAD_TIME_MINUTES),
    )

    async with database_session_factory() as session:
        persisted = await session.get(CollectionRequest, request.request_id)
    assert accepted.processing_status == EVENT_PROCESSED
    assert frozen.transitioned_request_count == 1
    assert persisted is not None and persisted.status == REQUEST_PRE_PLANNING
    assert persisted.planning_batch_id == frozen.batch.planning_batch_id  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_freeze_lock_wins_and_payment_cannot_leak_into_frozen_population(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tirodhan.modules.planning import service as planning_service

    user = await create_user(database_session_factory)
    now = utc_now().replace(microsecond=0)
    slot_start = now + timedelta(minutes=10)
    slot_end = slot_start + timedelta(minutes=30)
    anchor, _, _ = await create_request_record(
        database_session_factory,
        user.user_id,
        cell_id="freeze-first-cell",
        slot_start=slot_start,
        slot_end=slot_end,
        status=REQUEST_ACCEPTED,
    )
    pending, payment, attempt = await create_request_record(
        database_session_factory,
        user.user_id,
        cell_id="freeze-first-cell",
        slot_start=slot_start,
        slot_end=slot_end,
        status=REQUEST_PENDING_PAYMENT,
        with_payment=True,
    )
    assert payment is not None and attempt is not None
    acquired = asyncio.Event()
    release = asyncio.Event()

    async def held_lock(session: AsyncSession, **kwargs: Any) -> int:
        key = await acquire_work_unit_advisory_lock(session, **kwargs)
        acquired.set()
        await release.wait()
        return key

    monkeypatch.setattr(planning_service, "acquire_work_unit_advisory_lock", held_lock)
    freeze_task = asyncio.create_task(
        freeze_planning_batch(
            database_session_factory,
            PlanningWorkUnit("freeze-first-cell", slot_start, slot_end),
            lead_time_minutes=LEAD_TIME_MINUTES,
            max_attempts=MAX_ATTEMPTS,
            now=now,
        )
    )
    await acquired.wait()
    payment_task = asyncio.create_task(
        process_authenticated_payment_event(
            database_session_factory,
            successful_event(attempt, "freeze-first-success"),
            payload_hash=hashlib.sha256(b"freeze-first").digest(),
            planning_lead_time_minutes=LEAD_TIME_MINUTES,
        )
    )
    await asyncio.sleep(0)
    release.set()
    frozen = await freeze_task
    provider_event = await payment_task

    async with database_session_factory() as session:
        frozen_anchor = await session.get(CollectionRequest, anchor.request_id)
        still_pending = await session.get(CollectionRequest, pending.request_id)
        logical_payment = await session.get(Payment, payment.payment_id)
        durable_attempt = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        acceptance_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.event_type == "CollectionRequestAccepted"
            )
        )

    assert frozen.transitioned_request_count == 1
    assert frozen_anchor is not None and frozen_anchor.status == REQUEST_PRE_PLANNING
    assert still_pending is not None and still_pending.status == REQUEST_PENDING_PAYMENT
    assert logical_payment is not None and logical_payment.status == PAYMENT_PENDING
    assert durable_attempt is not None and durable_attempt.status == ATTEMPT_SUCCEEDED
    assert provider_event.processing_status == EVENT_RECONCILIATION
    assert provider_event.failure_code == "WORK_UNIT_FROZEN"
    assert acceptance_count == 0


@pytest.mark.asyncio
async def test_attempt_preparation_is_concurrent_redelivery_safe_and_pii_free(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    slot_start = utc_now().replace(microsecond=0) + timedelta(minutes=10)
    batch = await create_ready_batch(
        database_session_factory,
        user.user_id,
        cell_id="attempt-cell",
        slot_start=slot_start,
    )
    message = PlanningMessage(
        message_id="planning-message-1",
        planning_batch_id=batch.planning_batch_id,
    )

    first, concurrent_duplicate = await asyncio.gather(
        prepare_planning_attempt(database_session_factory, message),
        prepare_planning_attempt(database_session_factory, message),
    )
    redelivery = await prepare_planning_attempt(database_session_factory, message)

    async with database_session_factory() as session:
        attempts = list(
            await session.scalars(
                select(PlanningBatchAttempt).where(
                    PlanningBatchAttempt.planning_batch_id == batch.planning_batch_id
                )
            )
        )
        inbox = await session.get(
            InboxMessage,
            ("planning-worker", message.message_id),
        )
        outbox = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == PLANNING_BATCH_READY_MESSAGE)
        )

    assert {first.created, concurrent_duplicate.created} == {False, True}
    assert first.attempt is not None
    assert concurrent_duplicate.attempt is not None
    assert (
        first.attempt.planning_batch_attempt_id
        == concurrent_duplicate.attempt.planning_batch_attempt_id
    )
    assert redelivery.attempt is not None
    assert redelivery.attempt.planning_batch_attempt_id == first.attempt.planning_batch_attempt_id
    assert redelivery.created is False
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].outcome == PLANNING_ATTEMPT_STARTED
    assert attempts[0].completed_at is None
    assert inbox is not None and inbox.status == INBOX_PROCESSING
    assert inbox.processed_at is None
    assert inbox.business_key == f"{batch.planning_batch_id}:1"
    assert outbox is not None
    metadata = f"{outbox.payload} {inbox.business_key} {inbox.message_type}".lower()
    assert "address" not in metadata
    assert "77.2090" not in metadata
    assert "28.6139" not in metadata
    assert "payment" not in metadata
