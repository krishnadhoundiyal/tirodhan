from __future__ import annotations

import asyncio
import logging

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusReceiveMode
from azure.servicebus.aio import ServiceBusClient

from tirodhan.core.config import get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.dispatch.consumer import handle_dispatch_delivery
from tirodhan.modules.dispatch.push import FcmPushNotificationPort
from tirodhan.modules.reliability.service_bus import AzureDispatchDelivery

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    if (
        not settings.service_bus_namespace
        or not settings.service_bus_namespace.endswith(".servicebus.windows.net")
        or not settings.rider_notification_queue_name
        or settings.service_bus_operation_timeout_seconds is None
        or settings.rider_offer_lifetime_seconds is None
    ):
        raise ValueError("rider notification runtime configuration is incomplete")
    push = FcmPushNotificationPort(settings)
    engine = create_database_engine(settings)
    try:
        async with (
            DefaultAzureCredential(
                managed_identity_client_id=settings.service_bus_managed_identity_client_id
            ) as credential,
            ServiceBusClient(
                fully_qualified_namespace=settings.service_bus_namespace,
                credential=credential,
                retry_total=0,
                logging_enable=False,
                socket_timeout=settings.service_bus_operation_timeout_seconds,
            ) as bus,
            bus.get_queue_receiver(
                queue_name=settings.rider_notification_queue_name,
                receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
                prefetch_count=0,
            ) as receiver,
        ):
            async for message in receiver:
                try:
                    await handle_dispatch_delivery(
                        AzureDispatchDelivery(receiver, message),
                        create_session_factory(engine),
                        lifetime_seconds=settings.rider_offer_lifetime_seconds,
                        push=push,
                    )
                except Exception:
                    logger.warning("rider_notification_delivery_failed")
    finally:
        try:
            await push.close()
        finally:
            await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
