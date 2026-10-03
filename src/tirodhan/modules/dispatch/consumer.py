"""Resumable notification work. Neither broker settlement nor push runs in a DB transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch.cohorts import create_offer_cohort
from tirodhan.modules.dispatch.events import (
    DISPATCH_REQUESTED,
    DispatchMessage,
    InvalidDispatchMessageError,
    parse_dispatch_message,
)
from tirodhan.modules.dispatch.models import (
    AssignmentOffer,
    OfferNotificationDelivery,
    PushRegistration,
)
from tirodhan.modules.dispatch.push import (
    PushNotification,
    PushNotificationPort,
    PushRecipient,
    PushUnavailableError,
)
from tirodhan.modules.dispatch.service import (
    CollectionGroupNotFoundError,
    _active_assignment,
    _lock_group,
)
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.reliability.models import InboxMessage
from tirodhan.modules.reliability.primitives import (
    InboxMessageConflictError,
    claim_inbox_message,
    complete_inbox_message,
)

CONSUMER = "rider-dispatch-notifications"


class DispatchDelivery(Protocol):
    message_id: str
    message_type: str
    body: bytes

    async def complete(self) -> None: ...
    async def abandon(self) -> None: ...
    async def dead_letter(self) -> None: ...


@dataclass(frozen=True)
class PreparedDispatch:
    message_id: str
    message: DispatchMessage
    offer_ids: tuple[UUID, ...]
    processed: bool


async def prepare_dispatch_message(
    factory: async_sessionmaker[AsyncSession],
    *,
    message_id: str,
    message_type: str,
    body: bytes,
    lifetime_seconds: int,
    evaluation_time: datetime | None = None,
) -> PreparedDispatch:
    try:
        transport_id = str(UUID(message_id))
        if message_type != DISPATCH_REQUESTED:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise InvalidDispatchMessageError("invalid dispatch identity") from None
    message = parse_dispatch_message(body)
    try:
        async with factory() as session, session.begin():
            claim = await claim_inbox_message(
                session,
                consumer_name=CONSUMER,
                message_id=transport_id,
                message_type=DISPATCH_REQUESTED,
                business_key=f"{message.collection_group_id}:{message.dispatch_stage}",
            )
            # Serialize inbox reads/writes too; PROCESSING is resumable, not a lease.
            inbox = await session.scalar(
                select(InboxMessage)
                .where(
                    InboxMessage.consumer_name == CONSUMER, InboxMessage.message_id == transport_id
                )
                .with_for_update()
            )
            assert inbox is not None
            if inbox.status == "PROCESSED":
                return PreparedDispatch(transport_id, message, (), True)
            group = await _lock_group(session, message.collection_group_id)
            batch = await session.get(PlanningBatch, group.planning_batch_id)
            if (
                batch is None
                or group.planning_batch_id != message.planning_batch_id
                or (batch.cell_id, batch.slot_start, batch.slot_end)
                != (message.cell_id, message.slot_start, message.slot_end)
            ):
                raise InvalidDispatchMessageError("dispatch facts do not match authoritative group")
            if await _active_assignment(session, group.collection_group_id, lock=True) is not None:
                await complete_inbox_message(session, inbox)
                return PreparedDispatch(transport_id, message, (), True)
            offers = await create_offer_cohort(
                session,
                collection_group_id=group.collection_group_id,
                dispatch_stage=message.dispatch_stage,
                evaluation_time=evaluation_time or utc_now(),
                lifetime_seconds=lifetime_seconds,
            )
            offer_ids = tuple(offer.offer_id for offer in offers)
            # Only the first preparation for a logical cohort snapshots recipients.
            # New transport IDs also reuse that same durable snapshot. Offers and
            # all pairs commit atomically; no deliveries means a valid empty set.
            if claim.claimed and offers:
                prior = await session.scalar(
                    select(InboxMessage).where(
                        InboxMessage.consumer_name == CONSUMER,
                        InboxMessage.business_key == inbox.business_key,
                        InboxMessage.message_id != transport_id,
                    )
                )
                if prior is None:
                    registrations = tuple(
                        await session.scalars(
                            select(PushRegistration).where(
                                PushRegistration.rider_id.in_([offer.rider_id for offer in offers]),
                                PushRegistration.revoked_at.is_(None),
                            )
                        )
                    )
                    for offer in offers:
                        for registration in registrations:
                            if registration.rider_id == offer.rider_id:
                                await session.execute(
                                    insert(OfferNotificationDelivery)
                                    .values(
                                        offer_notification_delivery_id=new_uuid7(),
                                        offer_id=offer.offer_id,
                                        push_registration_id=registration.push_registration_id,
                                        status="PENDING",
                                        attempt_count=0,
                                        created_at=utc_now(),
                                    )
                                    .on_conflict_do_nothing(
                                        index_elements=["offer_id", "push_registration_id"]
                                    )
                                )
            if not offer_ids:
                await complete_inbox_message(session, inbox)
            return PreparedDispatch(transport_id, message, offer_ids, not offer_ids)
    except InboxMessageConflictError:
        raise InvalidDispatchMessageError("inconsistent dispatch transport identity") from None


async def process_dispatch_message(
    factory: async_sessionmaker[AsyncSession],
    *,
    message_id: str,
    message_type: str,
    body: bytes,
    lifetime_seconds: int,
    push: PushNotificationPort,
) -> None:
    prepared = await prepare_dispatch_message(
        factory,
        message_id=message_id,
        message_type=message_type,
        body=body,
        lifetime_seconds=lifetime_seconds,
    )
    if prepared.processed:
        return
    async with factory() as session, session.begin():
        # No persistent lease: concurrent invocations may duplicate an ambiguous
        # push, never the cohort/assignment. Count each durably attempted batch.
        rows = (
            await session.execute(
                select(OfferNotificationDelivery, PushRegistration, AssignmentOffer)
                .join(
                    PushRegistration,
                    OfferNotificationDelivery.push_registration_id
                    == PushRegistration.push_registration_id,
                )
                .join(
                    AssignmentOffer, OfferNotificationDelivery.offer_id == AssignmentOffer.offer_id
                )
                .where(
                    OfferNotificationDelivery.offer_id.in_(prepared.offer_ids),
                    OfferNotificationDelivery.status == "PENDING",
                )
                .order_by(OfferNotificationDelivery.offer_notification_delivery_id)
                .with_for_update(of=OfferNotificationDelivery)
            )
        ).all()
        recipients: list[PushRecipient] = []
        registrations: dict[UUID, UUID] = {}
        round_number = 1
        for delivery, registration, offer in rows:
            round_number = offer.offer_round
            if registration.revoked_at is not None:
                delivery.status = "PERMANENTLY_FAILED"
                continue
            delivery.attempt_count += 1
            delivery.last_attempt_at = utc_now()
            recipients.append(
                PushRecipient(
                    delivery.offer_notification_delivery_id, registration.registration_token
                )
            )
            registrations[delivery.offer_notification_delivery_id] = (
                registration.push_registration_id
            )
    # Sessions (including read transactions) are closed before external I/O.
    results = (
        await push.send_batch(
            tuple(recipients), PushNotification(prepared.message.collection_group_id, round_number)
        )
        if recipients
        else ()
    )
    if {result.delivery_id for result in results} != {
        recipient.delivery_id for recipient in recipients
    } or len(results) != len(recipients):
        raise PushUnavailableError("push result does not cover the prepared batch")
    tokens = {recipient.delivery_id: recipient.registration_token for recipient in recipients}
    async with factory() as session, session.begin():
        for result in sorted(results, key=lambda value: value.delivery_id):
            delivery = await session.get(
                OfferNotificationDelivery, result.delivery_id, with_for_update=True
            )
            assert delivery is not None
            if delivery.status != "PENDING":
                continue
            if result.status not in ("SENT", "PERMANENTLY_FAILED", "PENDING"):
                raise PushUnavailableError("push returned an invalid status")
            delivery.status = result.status
            if result.status == "SENT":
                delivery.sent_at = utc_now()
                delivery.provider_message_id = result.provider_message_id
            elif result.status == "PERMANENTLY_FAILED":
                registration = await session.get(
                    PushRegistration, registrations[result.delivery_id], with_for_update=True
                )
                assert registration is not None
                # A device may have refreshed its token during the network call.
                if registration.registration_token == tokens[result.delivery_id]:
                    registration.revoked_at = utc_now()
                    registration.updated_at = registration.revoked_at
        await session.flush()
        pending = await session.scalar(
            select(OfferNotificationDelivery.offer_notification_delivery_id)
            .where(
                OfferNotificationDelivery.offer_id.in_(prepared.offer_ids),
                OfferNotificationDelivery.status == "PENDING",
            )
            .limit(1)
        )
        if pending is None:
            inbox = await session.get(
                InboxMessage, (CONSUMER, prepared.message_id), with_for_update=True
            )
            assert inbox is not None
            await complete_inbox_message(session, inbox)
    if pending is not None:
        raise PushUnavailableError("retryable push deliveries remain")


async def handle_dispatch_delivery(
    delivery: DispatchDelivery,
    factory: async_sessionmaker[AsyncSession],
    *,
    lifetime_seconds: int,
    push: PushNotificationPort,
) -> None:
    try:
        await process_dispatch_message(
            factory,
            message_id=delivery.message_id,
            message_type=delivery.message_type,
            body=delivery.body,
            lifetime_seconds=lifetime_seconds,
            push=push,
        )
    except (InvalidDispatchMessageError, CollectionGroupNotFoundError):
        await delivery.dead_letter()
        return
    except Exception:
        await delivery.abandon()
        raise
    await delivery.complete()
