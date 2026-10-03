from __future__ import annotations

import asyncio
import logging

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusReceiveMode
from azure.servicebus.aio import AutoLockRenewer, ServiceBusClient

from tirodhan.core.config import get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.payments.razorpay import razorpay_configured
from tirodhan.modules.payments.refund_consumer import handle_refund_delivery
from tirodhan.modules.payments.runtime import razorpay_runtime
from tirodhan.modules.reliability.service_bus import AzureRefundDelivery

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    if (
        not settings.service_bus_namespace
        or not settings.service_bus_namespace.endswith(".servicebus.windows.net")
        or not settings.refund_queue_name
        or settings.refund_lock_renewal_seconds is None
        or settings.service_bus_operation_timeout_seconds is None
        or not razorpay_configured(settings)
    ):
        raise ValueError("Refund worker runtime configuration is incomplete")
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
            razorpay_runtime(settings) as provider,
            AutoLockRenewer() as renewer,
            bus.get_queue_receiver(
                queue_name=settings.refund_queue_name,
                receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
                prefetch_count=0,
            ) as receiver,
        ):
            assert provider is not None
            async for message in receiver:
                renewer.register(
                    receiver,
                    message,
                    max_lock_renewal_duration=settings.refund_lock_renewal_seconds,
                )
                try:
                    await handle_refund_delivery(
                        AzureRefundDelivery(receiver, message),
                        create_session_factory(engine),
                        provider=provider,
                    )
                except Exception:
                    logger.warning("refund_delivery_failed")
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
