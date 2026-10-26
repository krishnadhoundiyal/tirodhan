"""Finite outbox history fan-out and durable push retry. No infrastructure is provisioned."""

import asyncio
import logging
import signal

from tirodhan.core.config import get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.customer_account.notifications import (
    ConfiguredDelivery,
    deliver_one,
    materialize,
)
from tirodhan.modules.customer_account.protection import CustomerDataProtection
from tirodhan.modules.customer_account.web_push_ownership import ProcessGroupOwner


async def run(web_push_owner: ProcessGroupOwner | None = None) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    batch = settings.outbox_publish_batch_size
    if batch is None or not 1 <= batch <= 500:
        raise ValueError("customer notification batch size must be configured within 1..500")
    engine = create_database_engine(settings)
    port = None
    try:
        factory = create_session_factory(engine)
        await materialize(factory, batch)
        if not settings.customer_fcm_enabled and not settings.customer_web_push_enabled:
            return
        protection = CustomerDataProtection(settings)
        port = ConfiguredDelivery(settings, web_push_owner=web_push_owner)
        for _ in range(batch):
            if not await deliver_one(factory, protection, port):
                break
    except Exception:
        # Provider/SQL exceptions may contain tokens, keys or payloads.
        logging.getLogger(__name__).error("customer_notification_run_failed")
        raise SystemExit(1) from None
    finally:
        if port is not None:
            await port.close()
        await engine.dispose()


async def supervised_run() -> None:
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    installed = False
    try:
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            installed = True
        except NotImplementedError:
            # Windows asyncio.run still cancels pending sends on ordinary loop shutdown.
            pass
        owner = ProcessGroupOwner() if get_settings().customer_web_push_enabled else None
        await run(owner)
    except asyncio.CancelledError:
        pass
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    asyncio.run(supervised_run())
