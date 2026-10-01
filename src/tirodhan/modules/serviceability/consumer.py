from __future__ import annotations

import json
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.customers.ports import AddressProtector
from tirodhan.modules.reliability.primitives import (
    INBOX_PROCESSED,
    InboxMessageConflictError,
    claim_inbox_message,
    complete_inbox_message,
)
from tirodhan.modules.serviceability.ports import CellIdDeriver, LocationResolver
from tirodhan.modules.serviceability.service import (
    ServiceabilityContextNotFoundError,
    resolve_serviceability,
)

CONSUMER_NAME = "serviceability-resolver"
MESSAGE_TYPE = "ServiceabilityRequested"


class InvalidServiceabilityMessageError(ValueError):
    pass


class ServiceabilityDelivery(Protocol):
    message_id: str
    message_type: str
    body: bytes

    async def complete(self) -> None: ...
    async def abandon(self) -> None: ...
    async def dead_letter(self) -> None: ...


def _identity(message_id: str, message_type: str, body: bytes) -> tuple[str, UUID]:
    try:
        if message_type != MESSAGE_TYPE or len(body) > 512:
            raise ValueError
        transport_id = str(UUID(message_id))
        data = json.loads(body)
        if not isinstance(data, dict) or set(data) != {"serviceability_context_id"}:
            raise ValueError
        context_id = UUID(data["serviceability_context_id"])
        return transport_id, context_id
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise InvalidServiceabilityMessageError("invalid serviceability message") from None


async def process_serviceability_message(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    message_id: str,
    message_type: str,
    body: bytes,
    protector: AddressProtector,
    location_resolver: LocationResolver,
    cell_id_deriver: CellIdDeriver,
) -> None:
    transport_id, context_id = _identity(message_id, message_type, body)
    try:
        async with session_factory() as session, session.begin():
            claim = await claim_inbox_message(
                session,
                consumer_name=CONSUMER_NAME,
                message_id=transport_id,
                message_type=MESSAGE_TYPE,
                business_key=str(context_id),
            )
            if claim.message.status == INBOX_PROCESSED:
                return
    except InboxMessageConflictError:
        raise InvalidServiceabilityMessageError("inconsistent serviceability message") from None

    await resolve_serviceability(
        session_factory,
        context_id=context_id,
        protector=protector,
        location_resolver=location_resolver,
        cell_id_deriver=cell_id_deriver,
    )
    async with session_factory() as session, session.begin():
        claim = await claim_inbox_message(
            session,
            consumer_name=CONSUMER_NAME,
            message_id=transport_id,
            message_type=MESSAGE_TYPE,
            business_key=str(context_id),
        )
        await complete_inbox_message(session, claim.message)


async def handle_serviceability_delivery(
    delivery: ServiceabilityDelivery,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    protector: AddressProtector,
    location_resolver: LocationResolver,
    cell_id_deriver: CellIdDeriver,
) -> None:
    try:
        await process_serviceability_message(
            session_factory,
            message_id=delivery.message_id,
            message_type=delivery.message_type,
            body=delivery.body,
            protector=protector,
            location_resolver=location_resolver,
            cell_id_deriver=cell_id_deriver,
        )
    except (InvalidServiceabilityMessageError, ServiceabilityContextNotFoundError):
        await delivery.dead_letter()
        return
    except Exception:
        await delivery.abandon()
        raise
    await delivery.complete()
