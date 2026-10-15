"""Finite, read-only retention inventory for an explicitly authorized archival review."""

import asyncio
import json

from tirodhan.core.config import Settings
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.db.values import utc_now
from tirodhan.modules.routing_operations.retention import retention_inventory


async def execute() -> dict[str, int]:
    engine = create_database_engine(Settings())
    try:
        return await retention_inventory(create_session_factory(engine), now=utc_now())
    finally:
        await engine.dispose()


def main() -> None:
    print(json.dumps(asyncio.run(execute()), sort_keys=True))


if __name__ == "__main__":
    main()
