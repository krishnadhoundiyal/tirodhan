"""Finite scanner; scheduling/hosting remains a deployment concern."""

from __future__ import annotations

import asyncio

from tirodhan.core.config import get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.dispatch.cohorts import scan_expired_fleet_cohorts


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    engine = create_database_engine(settings)
    try:
        await scan_expired_fleet_cohorts(create_session_factory(engine))
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
