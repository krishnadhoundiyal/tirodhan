"""Non-destructive operational-retention inventory. No legal/custody history purge."""

from datetime import datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from .dispatch_models import RouteActivation, RouteRevision, VehicleRoute
from .failure_models import RecoveryWitness
from .fleet_models import FleetCycle, FleetPositionAnchor
from .models import RoutingShadowRun, VehiclePosition


async def retention_inventory(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
) -> dict[str, int]:
    """Candidates for reviewed archival, not permission to destroy their facts.

    The original PostgreSQL recovery policy, immutable histories and all FKs remain intact.
    Keep live route diagnostics and last/anchored positions even beyond nominal age.
    """
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        live_run = exists().where(
            RouteActivation.run_id == RoutingShadowRun.run_id,
            RouteRevision.activation_id == RouteActivation.activation_id,
            VehicleRoute.route_id == RouteRevision.route_id,
            VehicleRoute.status != "CLOSED",
        )
        newer = aliased(VehiclePosition)
        latest = exists().where(FleetPositionAnchor.position_id == VehiclePosition.position_id)
        newer_position = exists().where(
            newer.vehicle_id == VehiclePosition.vehicle_id,
            newer.revision > VehiclePosition.revision,
        )
        live_recovery = exists().where(
            RouteActivation.recovery_witness_id == RecoveryWitness.witness_id,
            RouteRevision.activation_id == RouteActivation.activation_id,
            VehicleRoute.route_id == RouteRevision.route_id,
            VehicleRoute.status != "CLOSED",
        )
        counts = {
            "inactive_recovery_diagnostics_older_than_30_days": await session.scalar(
                select(func.count())
                .select_from(RecoveryWitness)
                .where(RecoveryWitness.created_at < now - timedelta(days=30), ~live_recovery)
            ),
            "closed_routes_older_than_90_days": await session.scalar(
                select(func.count())
                .select_from(VehicleRoute)
                .where(
                    VehicleRoute.status == "CLOSED",
                    VehicleRoute.closed_at < now - timedelta(days=90),
                )
            ),
            "inactive_diagnostics_older_than_30_days": await session.scalar(
                select(func.count())
                .select_from(RoutingShadowRun)
                .where(
                    RoutingShadowRun.created_at < now - timedelta(days=30),
                    RoutingShadowRun.status.in_(("SUCCEEDED", "FAILED", "STALE")),
                    ~live_run,
                )
            ),
            "unanchored_positions_older_than_7_days": await session.scalar(
                select(func.count())
                .select_from(VehiclePosition)
                .where(
                    VehiclePosition.observed_at < now - timedelta(days=7), ~latest, newer_position
                )
            ),
            "cycles_in_12_month_metric_window": await session.scalar(
                select(func.count())
                .select_from(FleetCycle)
                .where(FleetCycle.created_at >= now - timedelta(days=365))
            ),
        }
        return {key: int(value or 0) for key, value in counts.items()}
