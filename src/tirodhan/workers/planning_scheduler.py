from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from tirodhan.core.config import get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.planning.service import (
    discover_due_planning_work_units,
    freeze_planning_batch,
)

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)

    if settings.planning_lead_time_minutes is None:
        raise ValueError("planning_lead_time_minutes must be configured")
    if settings.planning_max_attempts is None:
        raise ValueError("planning_max_attempts must be configured")
    if settings.planning_compaction_distance_m is None:
        raise ValueError("planning_compaction_distance_m must be configured")
    if settings.planning_max_group_requests is None:
        raise ValueError("planning_max_group_requests must be configured")

    engine = create_database_engine(settings, use_null_pool=True)
    try:
        session_factory = create_session_factory(engine)
        now = datetime.now(timezone.utc)

        work_units = await discover_due_planning_work_units(
            session_factory,
            lead_time_minutes=settings.planning_lead_time_minutes,
            now=now,
        )

        for work_unit in work_units:
            try:
                await freeze_planning_batch(
                    session_factory,
                    work_unit,
                    lead_time_minutes=settings.planning_lead_time_minutes,
                    max_attempts=settings.planning_max_attempts,
                    compaction_distance_m=settings.planning_compaction_distance_m,
                    max_group_requests=settings.planning_max_group_requests,
                    now=now,
                )
            except Exception as e:
                logger.exception("Failed to freeze planning batch for work unit", exc_info=e)

    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
