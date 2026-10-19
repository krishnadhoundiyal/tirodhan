"""Finite event/reconciliation job for projected slots; no road process is required."""

import asyncio

from tirodhan.core.config import Settings
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.db.values import utc_now
from tirodhan.modules.routing_operations.slot_projection import due_regions, refresh_slots


async def execute(settings: Settings, *, limit: int = 16) -> int:
    if not settings.routing_orchestration_enabled or not 1 <= limit <= 1000:
        raise ValueError("SLOT_RECONCILIATION_DISABLED_OR_INVALID_LIMIT")
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            regions = await due_regions(session, utc_now(), limit=limit)
        for region in regions:
            await refresh_slots(factory, region, now=utc_now())
        return len(regions)
    finally:
        await engine.dispose()


def main() -> None:
    # Existing job runner may invoke this repeatedly; no infrastructure is provisioned.
    try:
        count = asyncio.run(execute(Settings()))
    except Exception:
        raise SystemExit("SLOT_RECONCILIATION_RETRY_REQUIRED") from None
    print(f"REGIONAL_SLOT_REFRESHES={count}")


if __name__ == "__main__":
    main()
