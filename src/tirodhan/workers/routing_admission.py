"""Finite regional commercial preparation; no dispatch authority or infrastructure."""

from __future__ import annotations

from uuid import UUID

from tirodhan.core.config import Settings
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.workers.routing_regional import LocalRoadProfile


async def execute(
    region_id: UUID,
    profiles: list[LocalRoadProfile],
    settings: Settings,
    *,
    max_intents: int = 256,
    max_wall_seconds: int = 120,
) -> int:
    # Retired, including already queued intents: never start a graph or solver.
    # Existing successful reservations remain bindable under their original terms.
    raise OperationalInputError("COMMERCIAL_ACCEPTANCE_RETIRED_USE_SLOT_HOLDS")


def main() -> None:
    print("COMMERCIAL_ACCEPTANCE_RETIRED_USE_SLOT_HOLDS")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
