from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.payments.models import Refund
from tirodhan.modules.payments.ports import RefundProvider
from tirodhan.modules.payments.refunds import RefundConflictError, execute_refund_provider_call
from tirodhan.modules.reliability.primitives import (
    INBOX_PROCESSED,
    InboxMessageConflictError,
    claim_inbox_message,
    complete_inbox_message,
)
from tirodhan.modules.serviceability.consumer import ServiceabilityDelivery

CONSUMER_NAME = "refund-execution"
MESSAGE_TYPE = "RefundRequested"


class InvalidRefundMessageError(ValueError):
    pass


def refund_identity(message_id: str, message_type: str, body: bytes) -> tuple[str, UUID]:
    try:
        if message_type != MESSAGE_TYPE or len(body) > 512:
            raise ValueError
        transport_id = str(UUID(message_id))
        data = json.loads(body)
        if not isinstance(data, dict) or set(data) != {"refund_id"}:
            raise ValueError
        return transport_id, UUID(data["refund_id"])
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise InvalidRefundMessageError("invalid refund message") from None


async def process_refund_message(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    message_id: str,
    message_type: str,
    body: bytes,
    provider: RefundProvider,
) -> None:
    transport_id, refund_id = refund_identity(message_id, message_type, body)
    try:
        async with session_factory() as session, session.begin():
            claim = await claim_inbox_message(
                session,
                consumer_name=CONSUMER_NAME,
                message_id=transport_id,
                message_type=MESSAGE_TYPE,
                business_key=str(refund_id),
            )
            if claim.message.status == INBOX_PROCESSED:
                return
            if await session.get(Refund, refund_id) is None:
                raise InvalidRefundMessageError("refund message references missing intent")
        # PROCESSING inbox resumes; domain refund state prevents a second financial invocation.
        await execute_refund_provider_call(session_factory, refund_id, provider)
        async with session_factory() as session, session.begin():
            refund = await session.get(Refund, refund_id)
            if refund is None or refund.status not in {
                "SUCCEEDED",
                "FAILED",
                "SUBMITTED",
                "INITIATION_UNCERTAIN",
            }:
                raise RefundConflictError("refund execution has no durable settlement outcome")
            claim = await claim_inbox_message(
                session,
                consumer_name=CONSUMER_NAME,
                message_id=transport_id,
                message_type=MESSAGE_TYPE,
                business_key=str(refund_id),
            )
            await complete_inbox_message(session, claim.message)
    except InboxMessageConflictError:
        raise InvalidRefundMessageError("inconsistent refund message identity") from None


async def handle_refund_delivery(
    delivery: ServiceabilityDelivery,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: RefundProvider,
) -> None:
    try:
        await process_refund_message(
            session_factory,
            message_id=delivery.message_id,
            message_type=delivery.message_type,
            body=delivery.body,
            provider=provider,
        )
    except InvalidRefundMessageError:
        await delivery.dead_letter()
        return
    except Exception:
        await delivery.abandon()
        raise
    await delivery.complete()
