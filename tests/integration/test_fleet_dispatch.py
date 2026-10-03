from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from test_rider_dispatch import create_fixture

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch.cohorts import create_offer_cohort, scan_expired_fleet_cohorts
from tirodhan.modules.dispatch.consumer import (
    CONSUMER,
    prepare_dispatch_message,
    process_dispatch_message,
)
from tirodhan.modules.dispatch.events import (
    DISPATCH_REQUESTED,
    DispatchMessage,
)
from tirodhan.modules.dispatch.models import (
    AssignmentOffer,
    Fleet,
    FleetMembership,
    FleetServiceCell,
    OfferNotificationDelivery,
    PushRegistration,
    RiderAssignment,
    RiderAssignmentItem,
    RiderAvailability,
    RiderProfile,
    RiderServiceCell,
)
from tirodhan.modules.dispatch.push import PushDeliveryResult, PushUnavailableError
from tirodhan.modules.dispatch.service import (
    AssignmentOfferExpiredError,
    GroupAlreadyAssignedError,
    accept_assignment_offer,
    assign_group_manually,
)
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.planning.models import CollectionGroup, PlanningBatch
from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
CELL = "8764a9d4dffffff"


async def fixture(factory, *, fleet_count=2, independent_count=1, group_count=1):
    value = await create_fixture(
        factory, rider_count=fleet_count + independent_count, group_count=group_count
    )
    now = utc_now()
    async with factory() as session, session.begin():
        await session.execute(update(PlanningBatch).values(cell_id=CELL))
        for rider in value.rider_ids[:fleet_count]:
            fleet = Fleet(name="Synthetic fleet", status="ACTIVE")
            session.add(fleet)
            await session.flush()
            session.add_all(
                [
                    FleetMembership(fleet_id=fleet.fleet_id, rider_id=rider, joined_at=now),
                    FleetServiceCell(fleet_id=fleet.fleet_id, cell_id=CELL, activated_at=now),
                ]
            )
        for rider in value.rider_ids[fleet_count:]:
            session.add(RiderServiceCell(rider_id=rider, cell_id=CELL, activated_at=now))
    return value


async def message_for(factory, group_id, stage="FLEET_FIRST"):
    async with factory() as session:
        group = await session.get(CollectionGroup, group_id)
        batch = await session.get(PlanningBatch, group.planning_batch_id)
        return DispatchMessage(
            group_id,
            batch.planning_batch_id,
            batch.cell_id,
            batch.slot_start,
            batch.slot_end,
            stage,
        )


async def cohort(factory, group_id, *, stage="FLEET_FIRST", now=None):
    async with factory() as session, session.begin():
        return await create_offer_cohort(
            session,
            collection_group_id=group_id,
            dispatch_stage=stage,
            evaluation_time=now or utc_now(),
            lifetime_seconds=60,
        )


async def devices(factory, rider_id, *, count=1):
    async with factory() as session, session.begin():
        registrations = [
            PushRegistration(
                rider_id=rider_id,
                client_device_id=f"device-{index}",
                provider="FCM",
                platform="ANDROID",
                registration_token=f"private-token-{rider_id}-{index}",
            )
            for index in range(count)
        ]
        session.add_all(registrations)
    return registrations


class Push:
    def __init__(self, engine=None, outcomes=()):
        self.engine = engine
        self.outcomes = outcomes
        self.calls = []

    async def send_batch(self, recipients, notification):
        if self.engine:
            assert self.engine.pool.checkedout() == 0
            async with self.engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE "
                            "datname=current_database() AND pid<>pg_backend_pid() "
                            "AND state LIKE '%transaction%'"
                        )
                    )
                    == 0
                )
        self.calls.append((recipients, notification))
        return tuple(
            PushDeliveryResult(
                recipient.delivery_id,
                self.outcomes[index] if index < len(self.outcomes) else "SENT",
                "fcm-id",
            )
            for index, recipient in enumerate(recipients)
        )


async def process(factory, message, push, message_id=None):
    await process_dispatch_message(
        factory,
        message_id=str(message_id or new_uuid7()),
        message_type=DISPATCH_REQUESTED,
        body=json.dumps(message.payload()).encode(),
        lifetime_seconds=60,
        push=push,
    )


@pytest.mark.parametrize(
    "reason",
    [
        "inactive_fleet",
        "left",
        "wrong_cell",
        "offline",
        "busy",
        "suspended",
        "disabled",
        "revoked",
        "deactivated",
    ],
)
async def test_fleet_fresh_eligibility_exclusions(database_session_factory, reason):
    factory = database_session_factory
    value = await fixture(factory)
    excluded = value.rider_ids[1]
    async with factory() as session, session.begin():
        member = await session.scalar(
            select(FleetMembership).where(FleetMembership.rider_id == excluded)
        )
        if reason == "inactive_fleet":
            (await session.get(Fleet, member.fleet_id)).status = "INACTIVE"
        elif reason == "left":
            member.left_at = utc_now()
        elif reason in ("wrong_cell", "deactivated"):
            cell = await session.scalar(
                select(FleetServiceCell).where(FleetServiceCell.fleet_id == member.fleet_id)
            )
            if reason == "wrong_cell":
                cell.cell_id = "another-cell"
            else:
                cell.deactivated_at = utc_now()
        elif reason in ("offline", "busy"):
            availability = await session.get(RiderAvailability, excluded)
            if reason == "offline":
                availability.availability_intent = "OFFLINE"
            else:
                availability.work_state = "BUSY"
        elif reason == "suspended":
            (await session.get(RiderProfile, excluded)).status = "SUSPENDED"
        elif reason == "disabled":
            (await session.get(AppUser, excluded)).status = "DISABLED"
        else:
            role = await session.scalar(select(UserRole).where(UserRole.user_id == excluded))
            role.revoked_at = utc_now()
    offers = await cohort(factory, value.group_ids[0])
    assert {offer.rider_id for offer in offers} == {value.rider_ids[0]}
    assert all(offer.audience_kind == "FLEET" and offer.fleet_id for offer in offers)


async def test_complete_fleet_cohort_common_timestamp_and_historical_replay(
    database_session_factory,
):
    factory = database_session_factory
    value = await fixture(factory, fleet_count=3)
    now = utc_now()
    offers = await cohort(factory, value.group_ids[0], now=now)
    assert len(offers) == 3
    assert {offer.offered_at for offer in offers} == {now}
    assert {offer.expires_at for offer in offers} == {now + timedelta(seconds=60)}
    assert {offer.offer_round for offer in offers} == {1}
    async with factory() as session, session.begin():
        await session.execute(
            update(AppUser).where(AppUser.user_id.in_(value.rider_ids)).values(status="DISABLED")
        )
    replay = await cohort(factory, value.group_ids[0], now=now + timedelta(seconds=5))
    assert {offer.offer_id for offer in replay} == {offer.offer_id for offer in offers}


async def test_no_eligible_fleet_independent_round_one_excludes_all_current_members(
    database_session_factory,
):
    factory = database_session_factory
    value = await fixture(factory, fleet_count=1, independent_count=2)
    async with factory() as session, session.begin():
        await session.execute(update(Fleet).values(status="INACTIVE"))
        session.add(
            RiderServiceCell(rider_id=value.rider_ids[0], cell_id=CELL, activated_at=utc_now())
        )
    offers = await cohort(factory, value.group_ids[0])
    assert {offer.rider_id for offer in offers} == set(value.rider_ids[1:])
    assert {offer.audience_kind for offer in offers} == {"INDEPENDENT"}
    assert {offer.offer_round for offer in offers} == {1}
    assert all(offer.fleet_id is None for offer in offers)


async def test_fleet_expiry_requests_only_one_independent_stage_and_no_more_rounds(
    database_session_factory,
):
    factory = database_session_factory
    value = await fixture(factory)
    now = utc_now() - timedelta(minutes=2)
    fleet_offers = await cohort(factory, value.group_ids[0], now=now)
    assert await scan_expired_fleet_cohorts(factory) == 1
    assert await scan_expired_fleet_cohorts(factory) == 0
    independent = await cohort(factory, value.group_ids[0], stage="INDEPENDENT")
    assert {offer.rider_id for offer in independent} == {value.rider_ids[-1]}
    assert {offer.offer_round for offer in independent} == {2}
    assert all(offer.offered_at == independent[0].offered_at for offer in independent)
    assert (
        await scan_expired_fleet_cohorts(factory, evaluation_time=utc_now() + timedelta(hours=1))
        == 0
    )
    assert {
        offer.offer_id for offer in await cohort(factory, value.group_ids[0], stage="INDEPENDENT")
    } == {offer.offer_id for offer in independent}
    with pytest.raises(AssignmentOfferExpiredError):
        await accept_assignment_offer(
            factory, offer_id=fleet_offers[0].offer_id, rider_id=fleet_offers[0].rider_id
        )


async def test_crash_after_prepare_resumes_entire_durable_device_set(
    database_session_factory, database_engine
):
    factory = database_session_factory
    value = await fixture(factory)
    active = await devices(factory, value.rider_ids[0], count=3)
    await devices(factory, value.rider_ids[1])
    async with factory() as session, session.begin():
        (
            await session.get(PushRegistration, active[-1].push_registration_id)
        ).revoked_at = utc_now()
    message = await message_for(factory, value.group_ids[0])
    transport_id = str(new_uuid7())
    prepared = await prepare_dispatch_message(
        factory,
        message_id=transport_id,
        message_type=DISPATCH_REQUESTED,
        body=json.dumps(message.payload()).encode(),
        lifetime_seconds=60,
    )
    async with factory() as session:
        assert (await session.get(InboxMessage, (CONSUMER, transport_id))).status == "PROCESSING"
        assert (
            await session.scalar(select(func.count()).select_from(OfferNotificationDelivery)) == 3
        )
    push = Push(database_engine)
    await process(factory, message, push, transport_id)
    assert len(push.calls) == 1 and len(push.calls[0][0]) == 3
    assert push.calls[0][1].data() == {
        "type": "ASSIGNMENT_OFFER",
        "collection_group_id": str(value.group_ids[0]),
        "offer_round": "1",
    }
    async with factory() as session:
        assert set(await session.scalars(select(AssignmentOffer.offer_id))) == set(
            prepared.offer_ids
        )
        assert set(await session.scalars(select(OfferNotificationDelivery.status))) == {"SENT"}
        assert (await session.get(InboxMessage, (CONSUMER, transport_id))).status == "PROCESSED"
    await process(factory, message, push, transport_id)
    assert len(push.calls) == 1


async def test_transient_pending_and_permanent_revocation_resume(database_session_factory):
    factory = database_session_factory
    value = await fixture(factory, fleet_count=1)
    await devices(factory, value.rider_ids[0], count=3)
    message = await message_for(factory, value.group_ids[0])
    transport_id = str(new_uuid7())
    push = Push(outcomes=("SENT", "PERMANENTLY_FAILED", "PENDING"))
    with pytest.raises(PushUnavailableError):
        await process(factory, message, push, transport_id)
    async with factory() as session:
        rows = list(await session.scalars(select(OfferNotificationDelivery)))
        assert {row.status for row in rows} == {"SENT", "PERMANENTLY_FAILED", "PENDING"}
        permanent = next(row for row in rows if row.status == "PERMANENTLY_FAILED")
        assert (await session.get(PushRegistration, permanent.push_registration_id)).revoked_at
        assert (await session.get(InboxMessage, (CONSUMER, transport_id))).status == "PROCESSING"
    await process(factory, message, Push(), transport_id)
    async with factory() as session:
        assert (await session.get(InboxMessage, (CONSUMER, transport_id))).status == "PROCESSED"
        assert sorted(await session.scalars(select(OfferNotificationDelivery.attempt_count))) == [
            1,
            1,
            2,
        ]


async def test_no_devices_preserves_offers_and_does_not_expand_snapshot_on_redelivery(
    database_session_factory,
):
    factory = database_session_factory
    value = await fixture(factory)
    message = await message_for(factory, value.group_ids[0])
    push = Push()
    await process(factory, message, push)
    await devices(factory, value.rider_ids[0])
    await process(factory, message, push)
    assert push.calls == []
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AssignmentOffer)) == 2
        assert (
            await session.scalar(select(func.count()).select_from(OfferNotificationDelivery)) == 0
        )


async def test_duplicate_consumers_create_one_cohort(database_session_factory):
    factory = database_session_factory
    value = await fixture(factory)
    await devices(factory, value.rider_ids[0])
    message = await message_for(factory, value.group_ids[0])
    push = Push()
    await asyncio.wait_for(
        asyncio.gather(process(factory, message, push), process(factory, message, push)), 10
    )
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AssignmentOffer)) == 2
        assert (
            await session.scalar(select(func.count()).select_from(OfferNotificationDelivery)) == 1
        )


async def test_two_riders_accept_one_fleet_cohort_one_winner(database_session_factory):
    factory = database_session_factory
    value = await fixture(factory)
    offers = await cohort(factory, value.group_ids[0])
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            *(
                accept_assignment_offer(factory, offer_id=offer.offer_id, rider_id=offer.rider_id)
                for offer in offers
            ),
            return_exceptions=True,
        ),
        10,
    )
    assert sum(isinstance(result, RiderAssignment) for result in outcomes) == 1
    assert sum(isinstance(result, GroupAlreadyAssignedError) for result in outcomes) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1
        assert await session.scalar(select(func.count()).select_from(RiderAssignmentItem)) == 2
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "RiderAssignmentCreated")
            )
            == 1
        )


async def test_manager_and_offer_acceptance_converge_same_rider(database_session_factory):
    factory = database_session_factory
    value = await fixture(factory)
    offer = (await cohort(factory, value.group_ids[0]))[0]
    assignments = await asyncio.wait_for(
        asyncio.gather(
            assign_group_manually(
                factory,
                collection_group_id=value.group_ids[0],
                rider_id=offer.rider_id,
                manager_user_id=value.manager_id,
            ),
            accept_assignment_offer(factory, offer_id=offer.offer_id, rider_id=offer.rider_id),
        ),
        10,
    )
    assert assignments[0].assignment_id == assignments[1].assignment_id
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(RiderAssignment)) == 1
        assert (await session.get(RiderAvailability, offer.rider_id)).version == 2


@pytest.mark.parametrize("winner", ["acceptance", "timeout"])
async def test_real_group_lock_serializes_fleet_acceptance_and_timeout(
    database_session_factory, monkeypatch, winner
):
    from tirodhan.modules.dispatch import cohorts as cohort_service
    from tirodhan.modules.dispatch import service as dispatch_service

    factory = database_session_factory
    value = await fixture(factory)
    offer = (await cohort(factory, value.group_ids[0]))[0]
    locked, release = asyncio.Event(), asyncio.Event()
    original = dispatch_service._lock_group

    async def hold(session, group_id):
        group = await original(session, group_id)
        locked.set()
        await asyncio.wait_for(release.wait(), 5)
        return group

    if winner == "acceptance":
        monkeypatch.setattr(dispatch_service, "_lock_group", hold)
        first = asyncio.create_task(
            accept_assignment_offer(factory, offer_id=offer.offer_id, rider_id=offer.rider_id)
        )
        await asyncio.wait_for(locked.wait(), 5)
        second = asyncio.create_task(
            scan_expired_fleet_cohorts(factory, evaluation_time=offer.expires_at)
        )
    else:
        async with factory() as session, session.begin():
            await session.execute(
                update(AssignmentOffer).values(
                    expires_at=utc_now() - timedelta(seconds=1),
                    offered_at=utc_now() - timedelta(minutes=2),
                )
            )
        monkeypatch.setattr(cohort_service, "_lock_group", hold)
        first = asyncio.create_task(scan_expired_fleet_cohorts(factory))
        await asyncio.wait_for(locked.wait(), 5)
        second = asyncio.create_task(
            accept_assignment_offer(factory, offer_id=offer.offer_id, rider_id=offer.rider_id)
        )
    await asyncio.sleep(0.1)
    assert not second.done()
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 10)
    async with factory() as session:
        assignment_count = await session.scalar(select(func.count()).select_from(RiderAssignment))
        fallback_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.event_key
                == f"collection-group-dispatch:{value.group_ids[0]}:INDEPENDENT"
            )
        )
    if winner == "acceptance":
        assert assignment_count == 1 and fallback_count == 0 and results[1] == 0
    else:
        assert assignment_count == 0 and fallback_count == 1
        assert isinstance(results[1], AssignmentOfferExpiredError)


async def test_ambiguous_push_after_preparation_retries_without_duplicate_offers(
    database_session_factory,
):
    factory = database_session_factory
    value = await fixture(factory)
    await devices(factory, value.rider_ids[0])
    message = await message_for(factory, value.group_ids[0])
    transport_id = str(new_uuid7())

    class AmbiguousPush(Push):
        async def send_batch(self, recipients, notification):
            await super().send_batch(recipients, notification)
            raise PushUnavailableError("ambiguous provider outcome")

    ambiguous = AmbiguousPush()
    with pytest.raises(PushUnavailableError):
        await process(factory, message, ambiguous, transport_id)
    retry = Push()
    await process(factory, message, retry, transport_id)
    assert len(ambiguous.calls) == 1 and len(retry.calls) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AssignmentOffer)) == 2
        delivery = await session.scalar(select(OfferNotificationDelivery))
        assert delivery.status == "SENT" and delivery.attempt_count == 2


async def test_invalid_old_token_result_does_not_revoke_new_device_token(database_session_factory):
    from tirodhan.modules.dispatch.devices import register_push_device

    factory = database_session_factory
    value = await fixture(factory)
    registration = (await devices(factory, value.rider_ids[0]))[0]
    message = await message_for(factory, value.group_ids[0])

    class UpdatingPush(Push):
        async def send_batch(self, recipients, notification):
            await register_push_device(
                factory,
                rider_id=value.rider_ids[0],
                client_device_id=registration.client_device_id,
                platform="ANDROID",
                registration_token="replacement-private-token",
            )
            return [
                PushDeliveryResult(recipient.delivery_id, "PERMANENTLY_FAILED")
                for recipient in recipients
            ]

    await process(factory, message, UpdatingPush())
    async with factory() as session:
        updated = await session.get(PushRegistration, registration.push_registration_id)
        assert (
            updated.registration_token == "replacement-private-token" and updated.revoked_at is None
        )


async def test_dispatch_settlement_waits_for_durable_result_and_transient_abandons(
    database_session_factory,
):
    from tirodhan.modules.dispatch.consumer import handle_dispatch_delivery

    factory = database_session_factory
    value = await fixture(factory)
    await devices(factory, value.rider_ids[0])
    message = await message_for(factory, value.group_ids[0])

    class Delivery:
        message_id = str(new_uuid7())
        message_type = DISPATCH_REQUESTED
        body = json.dumps(message.payload()).encode()

        def __init__(self):
            self.settlements = []

        async def complete(self):
            async with factory() as session:
                assert (
                    await session.get(InboxMessage, (CONSUMER, self.message_id))
                ).status == "PROCESSED"
            self.settlements.append("complete")

        async def abandon(self):
            self.settlements.append("abandon")

        async def dead_letter(self):
            self.settlements.append("dead-letter")

    delivery = Delivery()
    with pytest.raises(PushUnavailableError):
        await handle_dispatch_delivery(
            delivery, factory, lifetime_seconds=60, push=Push(outcomes=("PENDING",))
        )
    assert delivery.settlements == ["abandon"]
    await handle_dispatch_delivery(delivery, factory, lifetime_seconds=60, push=Push())
    assert delivery.settlements == ["abandon", "complete"]
