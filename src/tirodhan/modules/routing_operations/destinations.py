"""Finite authorized destination recovery; never implies a handover or custody change."""

from copy import deepcopy
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.contracts import OptimizerEngine, RouteOptimizer
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal

from .authority import RouteAuthorityError
from .commands import OperationalInputError
from .fleet_models import FleetControl
from .operational_commands import AuthorizeDischarge, execute_operational
from .orchestration_models import OperatingRegion, RegionReceivingPoint
from .rolling import SCOPE, FleetPolicy, ready_snapshot
from .snapshot import MatrixSource, OperationalSnapshot, build_problem, capture_snapshot


async def recover_destinations(
    factory: async_sessionmaker[AsyncSession],
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    now: datetime,
    limit: int = 16,
) -> int:
    async with factory() as session:
        control = await session.get(FleetControl, SCOPE)
        if control is None or not control.configuration.get("regional"):
            return 0
        policy = FleetPolicy.model_validate(control.configuration)
        manager = control.manager_id
        regions = list(
            await session.scalars(
                select(OperatingRegion).order_by(OperatingRegion.region_id).limit(32)
            )
        )
        permissions = list((await session.execute(select(RegionReceivingPoint))).scalars())
    count = 0
    for region in regions:
        config = policy.snapshot_configuration(now.replace(microsecond=0)).model_copy(
            update={"region_id": region.region_id, "as_of": now}
        )
        snapshot = await capture_snapshot(factory, config)
        points = {p["receiving_point_id"]: p for p in snapshot.sources["points"]}
        authorized = {p.receiving_point_id for p in permissions if p.region_id == region.region_id}
        for request in snapshot.sources["requests"]:
            if count >= limit:
                return count
            current = points.get(request["receiving_point_id"])
            if request.get("hold") or (
                current
                and current["status"] == "ACTIVE"
                and request["discharge_deadline"] > now
                and not any(
                    r["receiving_point_id"] == current["receiving_point_id"]
                    and r["starts_at"] <= now < r["ends_at"]
                    for r in snapshot.sources["restrictions"]
                )
            ):
                continue
            # Feasible alternatives first, the expressly configured warehouse last.
            choices = sorted(
                authorized - {request["receiving_point_id"], region.default_warehouse_id}, key=str
            )
            if request["discharge_deadline"] <= now and current and current["status"] == "ACTIVE":
                choices.insert(0, request["receiving_point_id"])
            if region.default_warehouse_id != request["receiving_point_id"]:
                choices.append(region.default_warehouse_id)
            for destination in choices:
                if (
                    destination not in points
                    or points[destination]["status"] != "ACTIVE"
                    or not points[destination]["official_identifier"]
                ):
                    continue
                changed = deepcopy(snapshot.sources)
                changed_request = next(
                    r for r in changed["requests"] if r["request_id"] == request["request_id"]
                )
                # A closed shared facility can invalidate every owned lot on a vehicle.
                # Evaluate their replacement destinations together, not an infeasible partial view.
                for affected in changed["requests"]:
                    if affected["receiving_point_id"] == request[
                        "receiving_point_id"
                    ] and not affected.get("hold"):
                        affected["receiving_point_id"] = destination
                        if affected["discharge_deadline"] <= now:
                            affected["discharge_deadline"] = config.horizon_end
                changed_request["receiving_point_id"] = destination
                deadline = max(request["discharge_deadline"], config.horizon_end)
                changed_request["discharge_deadline"] = deadline
                for vehicle in changed["vehicles"]:
                    vehicle["resume_authorized"] = True
                try:
                    ready, _ = ready_snapshot(OperationalSnapshot(changed, b""), config, now)
                    if not any(
                        r["request_id"] == request["request_id"] for r in ready.sources["requests"]
                    ):
                        continue
                    problem = await build_problem(ready, config, new_uuid7(), providers, now=now)
                    feasible = False
                    for engine in OptimizerEngine:
                        try:
                            proposal = await optimizers[engine].propose(
                                problem, search_seconds=policy.search_seconds
                            )
                            validate_proposal(problem, proposal)
                            target = {
                                s.stop_id
                                for s in problem.stops
                                if s.lot_id == request["request_id"]
                            }
                            if target.isdisjoint(proposal.unserved_stop_ids):
                                feasible = True
                                break
                        except (RoutingAdapterError, RoutingValidationError):
                            continue
                    if not feasible:
                        continue
                    await execute_operational(
                        factory,
                        actor_id=manager,
                        command=AuthorizeDischarge(
                            client_command_id=new_uuid7(),
                            request_id=request["request_id"],
                            receiving_point_id=destination,
                            discharge_deadline=deadline,
                        ),
                        expires_at=now + timedelta(days=1),
                        now=now,
                    )
                    count += 1
                    # Recapture on the next finite tick. Activation still validates live facts.
                    break
                except (
                    OperationalInputError,
                    RouteAuthorityError,
                    RoutingAdapterError,
                    RoutingValidationError,
                ):
                    continue
    return count
