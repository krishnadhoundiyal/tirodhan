"""Independent outbox fan-out with inbox, owned history and leased device delivery."""

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from sqlalchemy import String, exists, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customer_account.models import (
    CustomerNotification,
    CustomerPreferences,
    CustomerPushBinding,
    CustomerPushDelivery,
)
from tirodhan.modules.customer_account.protection import CustomerDataProtection
from tirodhan.modules.customer_account.schemas import (
    BrowserSubscription,
    CustomerCollectionNotification,
    NotificationDto,
    NotificationPage,
)
from tirodhan.modules.customer_reads.cursor import Cursor, CursorCodec
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.identity.models import AppUser, RefreshSession, UserRole
from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent
from tirodhan.modules.reliability.primitives import claim_inbox_message, complete_inbox_message

if TYPE_CHECKING:
    from tirodhan.modules.dispatch.push import FcmPushNotificationPort

EVENT_TYPES = ("CollectionRequestAccepted", "CustomerFinancialStatusChanged")
CONSUMER = "customer-notification-history"


async def materialize(factory: async_sessionmaker[AsyncSession], limit: int = 100) -> int:
    if not 1 <= limit <= 500:
        raise ValueError("invalid batch size")
    async with factory() as session, session.begin():
        events = list(
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type.in_(EVENT_TYPES),
                    OutboxEvent.available_at <= utc_now(),
                    ~exists().where(
                        InboxMessage.consumer_name == CONSUMER,
                        InboxMessage.message_id == OutboxEvent.outbox_event_id.cast(String),
                    ),
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.outbox_event_id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        requests = {
            e.aggregate_id: await session.get(CollectionRequest, e.aggregate_id) for e in events
        }
        for owner in sorted({r.customer_id for r in requests.values() if r is not None}):
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": f"customer-notification-page:{owner}"},
            )
        for event in events:
            if (
                event.aggregate_type != "collection_request"
                or set(event.payload)
                != (
                    {"request_id"}
                    if event.event_type == EVENT_TYPES[0]
                    else {"request_id", "change"}
                )
                or event.payload.get("request_id") != str(event.aggregate_id)
            ):
                raise ValueError("INVALID_CUSTOMER_EVENT")
            request = requests[event.aggregate_id]
            if request is None:
                raise ValueError("MISSING_CUSTOMER_EVENT_REQUEST")
            claim = await claim_inbox_message(
                session,
                consumer_name=CONSUMER,
                message_id=str(event.outbox_event_id),
                message_type=event.event_type,
                business_key=str(request.request_id),
            )
            if claim.message.status == "PROCESSED":
                continue
            # Business replay also survives inbox retention/recovery independently of transport.
            established = await session.scalar(
                select(CustomerNotification).where(
                    CustomerNotification.source_event_id == event.outbox_event_id
                )
            )
            if established is not None:
                if (
                    established.request_id != request.request_id
                    or established.user_id != request.customer_id
                ):
                    raise ValueError("INCONSISTENT_CUSTOMER_EVENT_OWNER")
                await complete_inbox_message(session, claim.message)
                continue
            pref = await session.get(CustomerPreferences, request.customer_id)
            hindi = pref is not None and pref.language == "hi-IN"
            notification = CustomerNotification(
                source_event_id=event.outbox_event_id,
                user_id=request.customer_id,
                request_id=request.request_id,
                event_type=event.event_type,
                title="संग्रह अपडेट" if hindi else "Collection update",
                body="नवीनतम स्थिति देखने के लिए अपना संग्रह खोलें।"
                if hindi
                else "Open your collection to see its latest status.",
                created_at=utc_now(),
            )
            session.add(notification)
            await session.flush()
            if pref is None or pref.collection_notifications:
                bindings = await session.stream_scalars(
                    select(CustomerPushBinding)
                    .where(
                        CustomerPushBinding.user_id == request.customer_id,
                        CustomerPushBinding.revoked_at.is_(None),
                        CustomerPushBinding.created_at <= event.created_at,
                    )
                    .execution_options(yield_per=100)
                )
                async for partition in bindings.partitions():
                    session.add_all(
                        [
                            CustomerPushDelivery(
                                notification_id=notification.notification_id,
                                user_id=request.customer_id,
                                binding_id=b.binding_id,
                                binding_generation=b.generation,
                            )
                            for b in partition
                        ]
                    )
                    await session.flush()
            await complete_inbox_message(session, claim.message)
        return len(events)


async def notification_page(
    session: AsyncSession,
    *,
    owner: UUID,
    limit: int,
    token: str | None,
    settings: Settings,
    now: datetime | None = None,
) -> NotificationPage:
    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key,0))"),
        {"key": f"customer-notification-page:{owner}"},
    )
    now = now or utc_now()
    if settings.customer_cursor_signing_key is None or settings.customer_cursor_ttl_seconds is None:
        raise CustomerReadError(503, "NOTIFICATIONS_UNAVAILABLE")
    try:
        codec = CursorCodec(settings.customer_cursor_signing_key.get_secret_value().encode())
    except ValueError:
        raise CustomerReadError(503, "NOTIFICATIONS_UNAVAILABLE") from None
    cursor = codec.decode(token, owner=owner, view="notifications", now=now) if token else None
    snapshot = cursor.snapshot if cursor else now
    expiry = (
        cursor.expires_at
        if cursor
        else now + timedelta(seconds=settings.customer_cursor_ttl_seconds)
    )
    conditions = []
    if cursor:
        conditions.append(
            tuple_(CustomerNotification.created_at, CustomerNotification.notification_id)
            < (cursor.created_at, cursor.request_id)
        )
    rows = list(
        await session.scalars(
            select(CustomerNotification)
            .join(
                CollectionRequest, CollectionRequest.request_id == CustomerNotification.request_id
            )
            .where(
                CustomerNotification.user_id == owner,
                CollectionRequest.customer_id == owner,
                CustomerNotification.created_at <= snapshot,
                *conditions,
            )
            .order_by(
                CustomerNotification.created_at.desc(), CustomerNotification.notification_id.desc()
            )
            .limit(limit + 1)
        )
    )
    result = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = result[-1]
        next_cursor = codec.encode(
            Cursor(owner, "notifications", snapshot, expiry, last.created_at, last.notification_id)
        )
    return NotificationPage(
        items=[
            NotificationDto(
                event_id=str(r.notification_id),
                title=r.title,
                body=r.body,
                created_at=r.created_at,
                request_id=r.request_id,
            )
            for r in result
        ],
        next_cursor=next_cursor,
    )


class CustomerDeliveryPort(Protocol):
    async def send(
        self,
        provider: str,
        secret: str,
        signal: CustomerCollectionNotification,
        browser_origin: str | None,
    ) -> str: ...


class ConfiguredDelivery:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fcm: FcmPushNotificationPort | None = None
        if settings.customer_fcm_enabled:
            from tirodhan.modules.customer_account.native_push import require_fcm
            from tirodhan.modules.dispatch.push import FcmPushNotificationPort as FcmAdapter

            require_fcm(settings)
            self.fcm = FcmAdapter(settings, http_timeout_seconds=10)

    async def send(
        self,
        provider: str,
        secret: str,
        signal: CustomerCollectionNotification,
        browser_origin: str | None,
    ) -> str:
        if provider == "FCM":
            if self.fcm is None:
                return "DISABLED"
            from tirodhan.modules.dispatch.push import PushRecipient

            result = (await self.fcm.send_batch([PushRecipient(signal.event_id, secret)], signal))[
                0
            ]
            return {"SENT": "SENT", "PERMANENTLY_FAILED": "INVALID_TOKEN", "PENDING": "RETRY"}[
                result.status
            ]
        if provider == "WEBPUSH":
            if (
                not self.settings.customer_web_push_enabled
                or browser_origin not in self.settings.customer_web_push_origins
            ):
                return "DISABLED"
            from tirodhan.modules.customer_account.web_push import send_web_push

            try:
                return await send_web_push(
                    BrowserSubscription.model_validate_json(secret),
                    {
                        "schema_version": 1,
                        "event_id": str(signal.event_id),
                        "target": "COLLECTION",
                        "request_id": str(signal.request_id),
                    },
                    self.settings,
                )
            except CustomerReadError as error:
                return "INVALID_TOKEN" if error.status == 422 else "DISABLED"
        return "DISABLED"

    async def close(self) -> None:
        if self.fcm:
            await self.fcm.close()


async def eligible(session: AsyncSession, delivery: CustomerPushDelivery) -> bool:
    binding = await session.get(CustomerPushBinding, delivery.binding_id)
    pref = await session.get(CustomerPreferences, delivery.user_id)
    if (
        binding is None
        or binding.revoked_at is not None
        or binding.generation != delivery.binding_generation
        or (pref is not None and not pref.collection_notifications)
    ):
        return False
    return bool(
        await session.scalar(
            select(RefreshSession.refresh_session_id)
            .join(AppUser, AppUser.user_id == RefreshSession.user_id)
            .where(
                RefreshSession.refresh_session_id == binding.session_id,
                RefreshSession.user_id == delivery.user_id,
                RefreshSession.revoked_at.is_(None),
                RefreshSession.expires_at > utc_now(),
                AppUser.status == "ACTIVE",
                exists().where(
                    UserRole.user_id == delivery.user_id,
                    UserRole.role_code == "CUSTOMER",
                    UserRole.revoked_at.is_(None),
                ),
            )
        )
    )


async def deliver_one(
    factory: async_sessionmaker[AsyncSession],
    protection: CustomerDataProtection,
    port: CustomerDeliveryPort,
) -> bool:
    from tirodhan.modules.dispatch.push import PushUnavailableError

    now = utc_now()
    lease = new_uuid7()
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(CustomerPushDelivery)
            .where(
                or_(
                    (CustomerPushDelivery.status == "PENDING")
                    & (CustomerPushDelivery.next_attempt_at <= now),
                    (CustomerPushDelivery.status == "SENDING")
                    & (CustomerPushDelivery.lease_until <= now),
                )
            )
            .order_by(CustomerPushDelivery.next_attempt_at, CustomerPushDelivery.delivery_id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return False
        if not await eligible(session, row):
            row.status = "SUPPRESSED"
            row.lease_token = row.lease_until = None
            return True
        binding = await session.get(CustomerPushBinding, row.binding_id)
        notification = await session.get(CustomerNotification, row.notification_id)
        assert binding is not None and notification is not None
        row.status = "SENDING"
        row.lease_token, row.lease_until = lease, now + timedelta(seconds=60)
        row.attempts += 1
        delivery_id = row.delivery_id
        secret = protection.decrypt(
            f"push:{binding.user_id}:{binding.client_device_id}", binding.token_encrypted
        )
        provider = binding.provider
        browser_origin = binding.browser_origin
        signal = CustomerCollectionNotification(
            notification.notification_id,
            notification.request_id,
            notification.title,
            notification.body,
        )
    try:
        outcome = await asyncio.wait_for(
            port.send(provider, secret, signal, browser_origin), timeout=30
        )
    except (PushUnavailableError, asyncio.TimeoutError):
        outcome = "RETRY"
    if outcome not in {"SENT", "INVALID_TOKEN", "PERMANENT_FAILURE", "DISABLED", "RETRY"}:
        raise ValueError("INVALID_CUSTOMER_DELIVERY_RESULT")
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(CustomerPushDelivery)
            .where(
                CustomerPushDelivery.delivery_id == delivery_id,
                CustomerPushDelivery.lease_token == lease,
                CustomerPushDelivery.lease_until > utc_now(),
            )
            .with_for_update()
        )
        if row is None:
            return True
        row.lease_token = row.lease_until = None
        row.failure_code = None if outcome == "SENT" else outcome
        row.status = (
            "SENT"
            if outcome == "SENT"
            else "PERMANENTLY_FAILED"
            if outcome in ("INVALID_TOKEN", "PERMANENT_FAILURE")
            else "PENDING"
        )
        row.next_attempt_at = utc_now() + timedelta(seconds=min(3600, 2 ** min(row.attempts, 12)))
        if outcome == "INVALID_TOKEN":
            binding = await session.get(CustomerPushBinding, row.binding_id, with_for_update=True)
            if (
                binding is not None
                and binding.revoked_at is None
                and binding.generation == row.binding_generation
            ):
                binding.revoked_at = utc_now()
    return True
