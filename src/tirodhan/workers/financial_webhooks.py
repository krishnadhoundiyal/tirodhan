"""Peek-Lock financial evidence consumer; no raw webhook body enters this queue."""

import asyncio
import logging

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusReceiveMode
from azure.servicebus.aio import AutoLockRenewer, ServiceBusClient

from tirodhan.core.config import get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.payments.runtime import razorpay_runtime
from tirodhan.modules.payments.webhook_queue import handle_webhook_delivery
from tirodhan.modules.reliability.service_bus import AzureFinancialWebhookDelivery

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    if (
        not settings.financial_webhook_queue_name
        or not settings.financial_webhook_receiver_identity_client_id
        or not (settings.service_bus_namespace or "").endswith(".servicebus.windows.net")
        or settings.financial_webhook_lock_renewal_seconds is None
        or settings.service_bus_operation_timeout_seconds is None
        or settings.command_idempotency_ttl_seconds is None
        or settings.planning_lead_time_minutes is None
        or not settings.razorpay_account_id
    ):
        raise ValueError("Financial webhook worker configuration is incomplete")
    engine = create_database_engine(settings)
    try:
        async with (
            DefaultAzureCredential(
                managed_identity_client_id=settings.financial_webhook_receiver_identity_client_id
            ) as credential,
            ServiceBusClient(
                fully_qualified_namespace=settings.service_bus_namespace or "",
                credential=credential,
                retry_total=0,
                logging_enable=False,
                socket_timeout=settings.service_bus_operation_timeout_seconds,
            ) as bus,
            razorpay_runtime(settings) as provider,
            AutoLockRenewer() as renewer,
            bus.get_queue_receiver(
                queue_name=settings.financial_webhook_queue_name,
                receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
                prefetch_count=0,
            ) as receiver,
        ):
            if provider is None:
                raise ValueError("Financial webhook account is not configured")
            async for message in receiver:
                renewer.register(
                    receiver,
                    message,
                    max_lock_renewal_duration=settings.financial_webhook_lock_renewal_seconds,
                )
                try:
                    await handle_webhook_delivery(
                        AzureFinancialWebhookDelivery(receiver, message),
                        create_session_factory(engine),
                        account_key=provider.account_key,
                        planning_lead_time_minutes=settings.planning_lead_time_minutes,
                        command_ttl_seconds=settings.command_idempotency_ttl_seconds,
                    )
                except Exception:
                    logger.warning("financial_webhook_delivery_failed")
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
