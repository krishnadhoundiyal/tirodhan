from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from tirodhan.core.config import get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.collection_requests.expiry import expire_pending_collection_requests

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)

    engine = create_database_engine(settings, use_null_pool=True)
    try:
        session_factory = create_session_factory(engine)
        now = datetime.now(timezone.utc)

        async with session_factory() as session, session.begin():
            expired_count = await expire_pending_collection_requests(session, now)
            logger.info("pending_payment_expiry_completed", extra={"expired_count": expired_count})

    except Exception as e:
        logger.error("pending_payment_expiry_failed", extra={"error_type": type(e).__name__})
        raise
    finally:
        await engine.dispose()


def main() -> None:
    import sys

    try:
        asyncio.run(run())
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
