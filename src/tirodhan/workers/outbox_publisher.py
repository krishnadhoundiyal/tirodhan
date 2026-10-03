from __future__ import annotations

import asyncio

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient

from tirodhan.core.config import get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.reliability.publisher import publish_outbox_batch
from tirodhan.modules.reliability.service_bus import AzureServiceBusPublisher
from tirodhan.workers.serviceability import validate_bus_settings


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    validate_bus_settings(settings)
    if settings.outbox_publish_batch_size is None:
        raise ValueError("Outbox batch size must be configured")
    engine = create_database_engine(settings)
    try:
        async with (
            DefaultAzureCredential(
                managed_identity_client_id=settings.service_bus_managed_identity_client_id
            ) as credential,
            ServiceBusClient(
                fully_qualified_namespace=settings.service_bus_namespace or "",
                credential=credential,
                retry_total=0,
                logging_enable=False,
                socket_timeout=settings.service_bus_operation_timeout_seconds,
            ) as bus,
        ):
            await publish_outbox_batch(
                create_session_factory(engine),
                AzureServiceBusPublisher(
                    bus,
                    timeout_seconds=settings.service_bus_operation_timeout_seconds or 1,
                ),
                serviceability_entity=settings.serviceability_queue_name or "",
                batch_size=settings.outbox_publish_batch_size,
                rider_notification_entity=settings.rider_notification_queue_name,
                refund_entity=settings.refund_queue_name,
            )
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
