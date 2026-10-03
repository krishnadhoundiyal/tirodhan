import json
import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.dispatch.models import OfferNotificationDelivery, PushRegistration
from tirodhan.modules.dispatch.service import create_offer_cohort
from tirodhan.modules.notifications.push import (
    PushNotification,
    PushNotificationPort,
    PushRecipient,
)
from tirodhan.modules.reliability.primitives import (
    claim_inbox_message,
    complete_inbox_message,
)

logger = logging.getLogger(__name__)


async def process_dispatch_message(
    session_factory: async_sessionmaker[AsyncSession],
    push_port: PushNotificationPort,
    message_id: str,
    payload_body: bytes,
    lifetime_seconds: int = 120,
) -> None:
    payload = json.loads(payload_body)
    collection_group_id = UUID(payload["collection_group_id"])
    cell_id = payload["cell_id"]
    dispatch_stage = payload["dispatch_stage"]
    business_key = f"{collection_group_id}:{dispatch_stage}"

    async with session_factory() as session, session.begin():
        inbox = await claim_inbox_message(
            session,
            consumer_name="dispatch_worker",
            message_id=message_id,
            message_type="CollectionGroupDispatchRequested",
            business_key=business_key,
        )
        if not inbox.claimed:
            return

        offers, _ = await create_offer_cohort(
            session,
            collection_group_id=collection_group_id,
            cell_id=cell_id,
            dispatch_stage=dispatch_stage,
            lifetime_seconds=lifetime_seconds,
        )

        rider_ids = [offer.rider_id for offer in offers]
        if not rider_ids:
            await complete_inbox_message(session, inbox.message)
            return

        registrations = (
            (
                await session.execute(
                    select(PushRegistration).where(
                        PushRegistration.rider_id.in_(rider_ids),
                        PushRegistration.revoked_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    recipients = [
        PushRecipient(
            push_registration_id=str(reg.push_registration_id),
            registration_token=reg.registration_token,
            platform=reg.platform,
        )
        for reg in registrations
    ]

    notification = PushNotification(
        title="New pickup opportunity",
        body="Open Tirodhan to review and accept.",
        data={
            "type": "ASSIGNMENT_OFFER",
            "collection_group_id": str(collection_group_id),
            "offer_round": str(offers[0].offer_round) if offers else "1",
        },
    )

    deliveries = await push_port.send_batch(recipients, notification)

    async with session_factory() as session, session.begin():
        inbox = await claim_inbox_message(
            session,
            consumer_name="dispatch_worker",
            message_id=message_id,
            message_type="CollectionGroupDispatchRequested",
            business_key=business_key,
        )
        for delivery in deliveries:
            rider_id = next(
                reg.rider_id
                for reg in registrations
                if str(reg.push_registration_id) == delivery.push_registration_id
            )
            offer_id = next(offer.offer_id for offer in offers if offer.rider_id == rider_id)

            record = OfferNotificationDelivery(
                offer_id=offer_id,
                push_registration_id=UUID(delivery.push_registration_id),
                status="SENT"
                if delivery.success
                else ("PERMANENTLY_FAILED" if delivery.permanently_failed else "PENDING"),
                attempt_count=1,
                last_attempt_at=datetime.utcnow(),
                sent_at=datetime.utcnow() if delivery.success else None,
                provider_message_id=delivery.provider_message_id,
            )
            session.add(record)

            if delivery.permanently_failed:
                reg = await session.scalar(
                    select(PushRegistration).where(
                        PushRegistration.push_registration_id == UUID(delivery.push_registration_id)
                    )
                )
                if reg:
                    reg.revoked_at = datetime.utcnow()

        await complete_inbox_message(session, inbox.message)
