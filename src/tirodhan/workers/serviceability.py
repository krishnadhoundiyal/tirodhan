from __future__ import annotations

import asyncio
import logging

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusReceiveMode
from azure.servicebus.aio import AutoLockRenewer, ServiceBusClient

from tirodhan.core.config import Settings, get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.reliability.service_bus import AzureServiceabilityDelivery
from tirodhan.modules.serviceability.consumer import handle_serviceability_delivery
from tirodhan.modules.serviceability.runtime import serviceability_runtime

logger = logging.getLogger(__name__)


def validate_bus_settings(settings: Settings) -> None:
    if (
        not settings.service_bus_namespace
        or not settings.service_bus_namespace.endswith(".servicebus.windows.net")
        or not settings.serviceability_queue_name
        or settings.service_bus_operation_timeout_seconds is None
    ):
        raise ValueError("Service Bus runtime configuration is incomplete")


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    validate_bus_settings(settings)
    if settings.serviceability_lock_renewal_seconds is None:
        raise ValueError("Serviceability lock renewal duration must be configured")
    if settings.google_maps_api_key is None or settings.google_maps_http_timeout_seconds is None:
        raise ValueError("Google Geocoding runtime must be configured for the worker")
    if (
        settings.address_encryption_keys is None
        or settings.address_encryption_active_key_id is None
    ):
        raise ValueError("Address protection must be configured for the worker")
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
            serviceability_runtime(settings) as runtime,
            AutoLockRenewer() as renewer,
            bus.get_queue_receiver(
                queue_name=settings.serviceability_queue_name or "",
                receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
                prefetch_count=0,
            ) as receiver,
        ):
            async for message in receiver:
                renewer.register(
                    receiver,
                    message,
                    max_lock_renewal_duration=settings.serviceability_lock_renewal_seconds,
                )
                try:
                    await handle_serviceability_delivery(
                        AzureServiceabilityDelivery(receiver, message),
                        create_session_factory(engine),
                        protector=runtime.protector,
                        location_resolver=runtime.resolver,
                        cell_id_deriver=runtime.cells,
                    )
                except Exception:
                    logger.warning("serviceability_delivery_failed")
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
