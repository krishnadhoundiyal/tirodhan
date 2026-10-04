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
            logger.info(f"Expired {expired_count} pending payment collection requests.")

    except Exception as e:
        logger.exception("Failed to expire pending payment collection requests", exc_info=e)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
