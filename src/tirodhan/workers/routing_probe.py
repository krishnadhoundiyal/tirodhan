"""Finite read-only preflight: load a road graph only when native work is necessary."""

import asyncio
import json

from sqlalchemy import select

from tirodhan.core.config import Settings
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.db.values import utc_now
from tirodhan.modules.routing_operations.fleet_models import FleetControl
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityAdmissionRequest,
    FleetWakeup,
    RouteAcknowledgment,
    RouteDelivery,
)
from tirodhan.modules.routing_operations.rolling import SCOPE, FleetPolicy, _should_evaluate
from tirodhan.modules.routing_operations.snapshot import capture_snapshot


async def execute(settings: Settings) -> dict[str, bool]:
    if not (
        settings.routing_fleet_enabled
        and settings.routing_dispatch_enabled
        and settings.routing_orchestration_enabled
    ):
        return {"enabled": False, "native_work_due": False, "delivery_check_due": False}
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    now = utc_now()
    try:
        async with factory() as session:
            control = await session.get(FleetControl, SCOPE)
            if control is None or not control.configuration.get("regional"):
                return {"enabled": True, "native_work_due": False, "delivery_check_due": False}
            policy = FleetPolicy.model_validate(control.configuration)
            admission = await session.scalar(
                select(CapacityAdmissionRequest.admission_id)
                .where(
                    (CapacityAdmissionRequest.status == "PENDING")
                    | (
                        (CapacityAdmissionRequest.status == "RUNNING")
                        & (CapacityAdmissionRequest.lease_until <= now)
                    ),
                    CapacityAdmissionRequest.expires_at > now,
                )
                .limit(1)
            )
            delivery = await session.scalar(
                select(RouteDelivery.revision_id)
                .outerjoin(
                    RouteAcknowledgment,
                    RouteAcknowledgment.revision_id == RouteDelivery.revision_id,
                )
                .where(
                    RouteAcknowledgment.revision_id.is_(None),
                    RouteDelivery.escalated_at.is_(None),
                    RouteDelivery.due_at <= now,
                )
                .limit(1)
            )
            wakeup = await session.get(FleetWakeup, SCOPE)
            wake_due = bool(
                wakeup and wakeup.generation > wakeup.acknowledged and wakeup.due_at <= now
            )
            owned = control.lease_until is not None and control.lease_until > now
            fleet_eligible = not owned and control.next_due_at <= now
        snapshot = await capture_snapshot(
            factory, policy.snapshot_configuration(now.replace(microsecond=0))
        )
        fleet_due = fleet_eligible and await _should_evaluate(factory, snapshot, policy, now)
        return {
            "enabled": True,
            "native_work_due": bool(admission or fleet_due or (wake_due and not owned)),
            "delivery_check_due": delivery is not None,
        }
    finally:
        await engine.dispose()


def main() -> None:
    print(json.dumps(asyncio.run(execute(Settings())), sort_keys=True))


if __name__ == "__main__":
    main()
