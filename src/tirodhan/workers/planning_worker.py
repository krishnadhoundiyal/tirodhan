from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import NEXT_AVAILABLE_SESSION, ServiceBusReceiveMode
from azure.servicebus.aio import AutoLockRenewer, ServiceBusClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.core.config import get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.planning.execution import execute_planning_attempt
from tirodhan.modules.planning.service import PlanningMessage
from tirodhan.modules.reliability.service_bus import AzurePlanningDelivery

logger = logging.getLogger(__name__)


async def handle_planning_delivery(
    delivery: AzurePlanningDelivery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    try:
        payload = json.loads(delivery.body)
        if not isinstance(payload, dict):
            raise ValueError("Payload must be a JSON object")

        if "planning_batch_id" not in payload:
            raise ValueError("Missing planning_batch_id")

        batch_id_str = payload["planning_batch_id"]
        attempt_number = payload.get("attempt_number", 1)

        if not isinstance(attempt_number, int):
            raise ValueError("attempt_number must be an integer")

        message = PlanningMessage(
            message_id=delivery.message_id,
            planning_batch_id=UUID(batch_id_str),
            attempt_number=attempt_number,
            message_type=delivery.message_type,
        )

        await execute_planning_attempt(session_factory, message)
        await delivery.complete()

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.error("planning_message_invalid", extra={"error_type": type(e).__name__})
        await delivery.dead_letter()
    except Exception as e:
        logger.error("planning_delivery_failed", extra={"error_type": type(e).__name__})
        await delivery.abandon()


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    if (
        not settings.service_bus_namespace
        or not settings.service_bus_namespace.endswith(".servicebus.windows.net")
        or not settings.planning_queue_name
        or settings.planning_lock_renewal_seconds is None
        or settings.service_bus_operation_timeout_seconds is None
    ):
        raise ValueError("Planning worker runtime configuration is incomplete")

    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

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
            AutoLockRenewer() as renewer,
        ):
            while True:
                try:
                    async with bus.get_queue_receiver(
                        queue_name=settings.planning_queue_name,
                        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
                        prefetch_count=0,
                        session_id=NEXT_AVAILABLE_SESSION,
                        max_wait_time=5,
                    ) as receiver:
                        # Register the session lock renewal once when the receiver/session is
                        # acquired.
                        renewer.register(
                            receiver,
                            receiver.session,
                            max_lock_renewal_duration=settings.planning_lock_renewal_seconds,
                        )
                        async for message in receiver:
                            try:
                                await handle_planning_delivery(
                                    AzurePlanningDelivery(receiver, message),
                                    session_factory,
                                )
                            except Exception:
                                logger.warning("planning_delivery_failed")
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    import azure.servicebus.exceptions

                    if isinstance(e, azure.servicebus.exceptions.OperationTimeoutError):
                        await asyncio.sleep(5)
                    else:
                        logger.warning(
                            "planning_session_acquisition_failed",
                            extra={"error_type": type(e).__name__},
                        )
                        await asyncio.sleep(5)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
