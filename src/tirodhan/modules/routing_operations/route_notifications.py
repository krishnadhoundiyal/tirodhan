"""Revision notifications reuse existing registrations/push; receipt remains authoritative."""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.dispatch.models import PushRegistration
from tirodhan.modules.dispatch.push import (
    PushNotificationPort,
    PushRecipient,
    PushUnavailableError,
    RouteRevisionNotification,
)
from tirodhan.modules.reliability.models import InboxMessage
from tirodhan.modules.reliability.primitives import claim_inbox_message, complete_inbox_message

from .dispatch_models import RouteRevision, VehicleRoute
from .failure_models import RoutePushDelivery


async def notify_revision(
    factory: async_sessionmaker[AsyncSession],
    message_id: str,
    body: bytes,
    push: PushNotificationPort,
) -> None:
    if len(body) > 2048:
        raise ValueError("INVALID_REVISION_NOTIFICATION")
    payload = json.loads(body)
    if not isinstance(payload, dict) or set(payload) != {"revision_id", "rider_id"}:
        raise ValueError("INVALID_REVISION_NOTIFICATION")
    revision_id, rider_id = UUID(payload["revision_id"]), UUID(payload["rider_id"])
    message_id = str(UUID(message_id))
    consumer = "route-revision-notifications"
    async with factory() as session, session.begin():
        claim = await claim_inbox_message(
            session,
            consumer_name=consumer,
            message_id=message_id,
            message_type="VehicleRouteDeliveryRequested",
            business_key=f"{revision_id}",
        )
        inbox = await session.get(InboxMessage, (consumer, message_id), with_for_update=True)
        assert inbox
        if inbox.status == "PROCESSED":
            return
        revision = await session.get(RouteRevision, revision_id)
        root = await session.get(VehicleRoute, revision.route_id) if revision else None
        from .models import OperatingShift

        shift = await session.get(OperatingShift, root.shift_id) if root else None
        if not root or not revision or not shift or shift.rider_id != rider_id:
            raise ValueError("REVISION_OWNER_MISMATCH")
        from .orchestration_models import RouteAcknowledgment

        if (
            root.status == "CLOSED"
            or root.version != revision.version
            or await session.get(RouteAcknowledgment, revision_id) is not None
        ):
            await complete_inbox_message(session, inbox)
            return
        registrations = list(
            await session.scalars(
                select(PushRegistration).where(
                    PushRegistration.rider_id == rider_id, PushRegistration.revoked_at.is_(None)
                )
            )
        )
        for registration in registrations:
            await session.execute(
                insert(RoutePushDelivery)
                .values(
                    revision_id=revision_id,
                    registration_id=registration.push_registration_id,
                    status="PENDING",
                    attempts=0,
                )
                .on_conflict_do_nothing()
            )
        if claim.claimed:
            from sqlalchemy import update

            await session.execute(
                update(RoutePushDelivery)
                .where(
                    RoutePushDelivery.revision_id == revision_id,
                    RoutePushDelivery.registration_id.in_(
                        [r.push_registration_id for r in registrations]
                    ),
                )
                .values(status="PENDING")
            )
        rows = (
            await session.execute(
                select(RoutePushDelivery, PushRegistration)
                .join(
                    PushRegistration,
                    RoutePushDelivery.registration_id == PushRegistration.push_registration_id,
                )
                .where(
                    RoutePushDelivery.revision_id == revision_id,
                    RoutePushDelivery.status == "PENDING",
                )
                .with_for_update(of=RoutePushDelivery)
            )
        ).all()
        recipients = []
        tokens = {}
        for delivery, registration in rows:
            if registration.revoked_at:
                delivery.status = "PERMANENTLY_FAILED"
                continue
            delivery.attempts += 1
            recipients.append(
                PushRecipient(registration.push_registration_id, registration.registration_token)
            )
            tokens[registration.push_registration_id] = registration.registration_token
    results = (
        await push.send_batch(recipients, RouteRevisionNotification(revision_id))
        if recipients
        else ()
    )
    if len(results) != len(recipients) or {r.delivery_id for r in results} != {
        r.delivery_id for r in recipients
    }:
        raise PushUnavailableError("INVALID_PUSH_RESULT_COVERAGE")
    async with factory() as session, session.begin():
        for result in results:
            delivery = await session.get(
                RoutePushDelivery, (revision_id, result.delivery_id), with_for_update=True
            )
            assert delivery
            if delivery.status != "PENDING":
                continue
            if result.status not in {"PENDING", "SENT", "PERMANENTLY_FAILED"}:
                raise PushUnavailableError("INVALID_PUSH_RESULT")
            delivery.status = result.status
            if result.status == "PERMANENTLY_FAILED":
                current_registration = await session.get(
                    PushRegistration, result.delivery_id, with_for_update=True
                )
                if (
                    current_registration
                    and current_registration.registration_token == tokens[result.delivery_id]
                ):
                    current_registration.revoked_at = current_registration.updated_at = utc_now()
        await session.flush()
        pending = await session.scalar(
            select(RoutePushDelivery.registration_id)
            .where(
                RoutePushDelivery.revision_id == revision_id, RoutePushDelivery.status == "PENDING"
            )
            .limit(1)
        )
        if pending is None:
            inbox = await session.get(InboxMessage, (consumer, message_id), with_for_update=True)
            assert inbox
            await complete_inbox_message(session, inbox)
    if pending is not None:
        raise PushUnavailableError("REVISION_DELIVERY_PENDING")
