"""Focused real OSRM/VROOM/OR-Tools replacement acceptance, not the full QA campaign.

The incumbent split is a declared fixture proposal, validated and activated through
the production authority. Replacement witnesses and subsequent activation use real
native optimizers. Bookings use actual holds, checkout and authenticated payment.
"""

import asyncio
import os
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from geographical_fixture import DATA, arrivals, operational, setup_region
from sqlalchemy import select
from test_geographical_rolling import ENABLED, geographical_requirements  # noqa: F401

from tirodhan.db.values import new_uuid7
from tirodhan.modules.business_admin.lineage import booking_lineage
from tirodhan.modules.planning.routing.adapters import (
    ORTOOLS_VERSION,
    VROOM_VERSION,
    ORToolsOptimizer,
    VroomOptimizer,
)
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ProposedRoute,
    RouteProposal,
    StopKind,
)
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.authority import RouteAuthorityError
from tirodhan.modules.routing_operations.capacity_models import RiderReplacement
from tirodhan.modules.routing_operations.dispatch_models import (
    RouteRevision,
    RouteTask,
    VehicleRoute,
)
from tirodhan.modules.routing_operations.parallel import WorkerResult, local_workers
from tirodhan.modules.routing_operations.regional_planner import run_region
from tirodhan.modules.routing_operations.replacement_commands import (
    DecideReplacement,
    ManagerRequestReplacement,
    RecommendReplacement,
)
from tirodhan.modules.routing_operations.replacement_models import ReplacementCandidate
from tirodhan.modules.routing_operations.replacement_reads import read_replacement_options
from tirodhan.modules.routing_operations.replacements import recommend_replacement

pytestmark = [
    pytest.mark.integration,
    pytest.mark.routing_engine,
    pytest.mark.asyncio,
    pytest.mark.skipif(not ENABLED, reason="explicit native replacement acceptance opt-in"),
]


class ObservedOptimizer:
    def __init__(self, native):
        self.native, self.problems = native, []

    async def propose(self, problem, *, search_seconds):
        self.problems.append(problem)
        return await self.native.propose(problem, search_seconds=search_seconds)


async def incumbent(factory, region, provider, owners):
    async def fixture_workers(tasks, processes):
        results = []
        for task in tasks:
            orders = {}
            for stop in sorted(
                task.problem.stops,
                key=lambda s: (
                    s.kind == StopKind.DISCHARGE,
                    s.service_start_window.start,
                    str(s.stop_id),
                ),
            ):
                vehicle = owners[stop.lot_id]
                if vehicle in {v.vehicle_id for v in task.problem.vehicles}:
                    orders.setdefault(vehicle, []).append(stop.stop_id)
            served = {s for order in orders.values() for s in order}
            proposal = RouteProposal(
                task.problem.problem_id,
                task.problem.snapshot_id,
                task.problem.contract_version,
                task.problem.policy_version,
                task.engine,
                VROOM_VERSION if task.engine == OptimizerEngine.VROOM else ORTOOLS_VERSION,
                tuple(ProposedRoute(v, tuple(order)) for v, order in orders.items()),
                tuple(s.stop_id for s in task.problem.stops if s.stop_id not in served),
            )
            results.append(WorkerResult(task_id=task.task_id, proposal=proposal, elapsed_seconds=0))
        return results

    cycle = await run_region(
        factory,
        region_id=region["region_id"],
        providers={"qa-car": provider},
        workers=fixture_workers,
        clock=lambda: region["clock"],
    )
    assert cycle.status == "ACTIVATED", cycle.failure_code


async def test_native_options_preserve_busy_candidate_work_and_activate(database_session_factory):
    factory = database_session_factory
    nodes = DATA["regions"]["North"]["nodes"]
    region = await setup_region(
        factory, "North", 4, 7200, public_nodes=nodes[:240] + [nodes[-4]] * 4
    )
    records = await arrivals(factory, region, 2, 1)
    rescue, existing = [UUID(r["booking_id"]) for r in records]
    async with httpx.AsyncClient() as client:
        provider = OSRMMatrixProvider(
            client=client,
            endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
            graph_version=DATA["source_sha256"],
            profile_version="qa-car",
            max_nodes=512,
            max_snap_distance_m=100,
            timeout_seconds=30,
            max_response_bytes=32_000_000,
        )
        await incumbent(
            factory,
            region,
            provider,
            {
                rescue: region["vehicles"][0],
                existing: region["vehicles"][1],
            },
        )
        async with factory() as session:
            route = await session.scalar(
                select(VehicleRoute).where(
                    VehicleRoute.vehicle_id == region["vehicles"][0],
                    VehicleRoute.status != "CLOSED",
                )
            )
            revision = await session.scalar(
                select(RouteRevision).where(
                    RouteRevision.route_id == route.route_id, RouteRevision.version == route.version
                )
            )
        command = ManagerRequestReplacement(
            client_command_id=new_uuid7(),
            request_id=rescue,
            reason="VEHICLE_UNAVAILABLE",
            expected_revision_id=revision.revision_id,
            include_route=True,
        )
        replacement = await operational(factory, region["manager"], command, region["clock"])
        assert (
            await operational(factory, region["manager"], command, region["clock"]) == replacement
        )
        optimizers = {
            OptimizerEngine.VROOM: ObservedOptimizer(VroomOptimizer()),
            OptimizerEngine.OR_TOOLS: ObservedOptimizer(ORToolsOptimizer()),
        }
        assert await recommend_replacement(
            factory,
            replacement,
            providers={"qa-car": provider},
            optimizers=optimizers,
            now=region["clock"],
        )
        assert all(o.problems for o in optimizers.values())
        assert all(
            {s.lot_id for s in p.stops} == {rescue, existing}
            for o in optimizers.values()
            for p in o.problems
        )
        async with factory() as session:
            view = await read_replacement_options(session, replacement, region["clock"])
            candidates = list(
                await session.scalars(
                    select(ReplacementCandidate).where(
                        ReplacementCandidate.evaluation_id == view["evaluation"]["evaluation_id"]
                    )
                )
            )
        assert len(candidates) >= 2, view
        chosen = next(c for c in candidates if c.shift_id == region["shifts"][1])
        assert [c["rank"] for c in view["candidates"]] == list(range(1, len(candidates) + 1))
        async with factory() as session:
            assert await read_replacement_options(session, replacement, region["clock"]) == view
        # A real new customer hold/checkout/capture changes regional authority after
        # recommendation. The old candidate must not release any existing work.
        late = await arrivals(factory, region, 1, 2)
        with pytest.raises(RouteAuthorityError, match="STALE"):
            await operational(
                factory,
                region["manager"],
                DecideReplacement(
                    client_command_id=new_uuid7(),
                    replacement_id=replacement,
                    expected_version=view["version"],
                    decision="APPROVE",
                    selected_candidate_id=chosen.candidate_id,
                    expected_source_version=view["evaluation"]["source_version"],
                    expected_region_version=view["evaluation"]["region_version"],
                ),
                region["clock"],
            )
        await operational(
            factory,
            region["manager"],
            RecommendReplacement(
                client_command_id=new_uuid7(),
                replacement_id=replacement,
                expected_version=view["version"],
            ),
            region["clock"],
        )
        assert await recommend_replacement(
            factory,
            replacement,
            providers={"qa-car": provider},
            optimizers=optimizers,
            now=region["clock"],
        )
        async with factory() as session:
            view = await read_replacement_options(session, replacement, region["clock"])
            chosen = await session.scalar(
                select(ReplacementCandidate).where(
                    ReplacementCandidate.evaluation_id == view["evaluation"]["evaluation_id"],
                    ReplacementCandidate.shift_id == region["shifts"][1],
                )
            )
        assert chosen is not None
        assert UUID(late[0]["booking_id"]) in {
            s.lot_id for s in optimizers[OptimizerEngine.VROOM].problems[-1].stops
        }
        selection = DecideReplacement(
            client_command_id=new_uuid7(),
            replacement_id=replacement,
            expected_version=view["version"],
            decision="APPROVE",
            selected_candidate_id=chosen.candidate_id,
            expected_source_version=view["evaluation"]["source_version"],
            expected_region_version=view["evaluation"]["region_version"],
        )
        results = await asyncio.gather(
            *(
                operational(
                    factory,
                    region["manager"],
                    selection.model_copy(update={"client_command_id": new_uuid7()}),
                    region["clock"],
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(r, Exception) for r in results) == 1, results
        assert all(
            not isinstance(r, Exception) or isinstance(r, RouteAuthorityError) for r in results
        )
        async with factory() as session:
            pending = await session.get(RiderReplacement, replacement)
            assert pending.status == "APPROVED" and pending.activation_id is None
        from test_manager_business import change

        from tirodhan.modules.business_admin.commands import RiderRoleEdit
        from tirodhan.modules.business_admin.history import current_version
        from tirodhan.modules.identity.models import UserRole

        async with factory() as session:
            role = await session.scalar(
                select(UserRole).where(
                    UserRole.user_id == region["riders"][1],
                    UserRole.role_code == "RIDER",
                    UserRole.revoked_at.is_(None),
                )
            )
            version = await current_version(session, "user_role", role.user_role_id)
        grant = RiderRoleEdit(
            client_command_id=new_uuid7(),
            resource_id=role.user_role_id,
            expected_version=version,
            reason="ELIGIBILITY_REVIEW",
            rider_id=region["riders"][1],
            active=False,
        )
        await change(factory, region["manager"], grant)
        region["clock"] += timedelta(seconds=1)
        await run_region(
            factory,
            region_id=region["region_id"],
            providers={"qa-car": provider},
            workers=local_workers,
            clock=lambda: region["clock"],
        )
        async with factory() as session:
            pending = await session.get(RiderReplacement, replacement)
            assert pending.status == "APPROVED" and pending.activation_id is None
            history = await booking_lineage(session, rescue)
            assert len(history["assignments"]) == 1  # No unauthorized substitute is invented.
        await change(
            factory,
            region["manager"],
            grant.model_copy(
                update={
                    "client_command_id": new_uuid7(),
                    "resource_id": new_uuid7(),
                    "expected_version": 0,
                    "active": True,
                }
            ),
        )
        from tirodhan.modules.routing_operations.operational_commands import ResumeRoute

        async with factory() as session:
            held = await session.scalar(
                select(VehicleRoute).where(
                    VehicleRoute.vehicle_id == region["vehicles"][1],
                    VehicleRoute.status == "RECOVERY_REQUIRED",
                )
            )
            current = (
                await session.scalar(
                    select(RouteRevision).where(
                        RouteRevision.route_id == held.route_id,
                        RouteRevision.version == held.version,
                    )
                )
                if held
                else None
            )
        if current:
            await operational(
                factory,
                region["manager"],
                ResumeRoute(
                    client_command_id=new_uuid7(),
                    revision_id=current.revision_id,
                ),
                region["clock"],
            )
        region["clock"] += timedelta(seconds=1)
        cycle = await run_region(
            factory,
            region_id=region["region_id"],
            providers={"qa-car": provider},
            workers=local_workers,
            clock=lambda: region["clock"],
        )
        assert cycle.status == "ACTIVATED", cycle.failure_code
        async with factory() as session:
            completed = await session.get(RiderReplacement, replacement)
            assert (
                completed.status == "COMPLETED" and completed.activation_id == cycle.activation_id
            )
            history = await booking_lineage(session, rescue)
            assert len(history["assignments"]) == 2
            assert history["assignments"][0]["rider_id"] == region["riders"][0]
            assert history["assignments"][0]["released_at"] is not None
            assert history["assignments"][1]["rider_id"] == region["riders"][1]
            assert (
                await session.scalar(
                    select(RouteTask.task_id).where(
                        RouteTask.kind == "PICKUP",
                        RouteTask.state == "PENDING",
                        RouteTask.pickup_execution_id
                        == (await booking_lineage(session, existing))["pickup_execution_id"],
                    )
                )
                is not None
            )


async def test_native_failed_cart_keeps_individual_custody_until_transfer_and_activation(
    database_session_factory,
):
    from geographical_execution import progress
    from geographical_fixture import resource

    from tirodhan.modules.routing_operations.commands import PositionReport
    from tirodhan.modules.routing_operations.operational_commands import (
        AcknowledgeRoute,
        CompleteTransfer,
        TransferReceipt,
        custodian,
    )
    from tirodhan.modules.routing_operations.physical import cargo_load
    from tirodhan.modules.routing_operations.replacement_commands import PrepareReplacementTransfer

    factory = database_session_factory
    nodes = DATA["regions"]["North"]["nodes"]
    region = await setup_region(
        factory, "North", 4, 7200, public_nodes=nodes[:240] + [nodes[-4]] * 4
    )
    records = await arrivals(factory, region, 3, 1)
    lots = [UUID(r["booking_id"]) for r in records]
    async with httpx.AsyncClient() as client:
        provider = OSRMMatrixProvider(
            client=client,
            endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
            graph_version=DATA["source_sha256"],
            profile_version="qa-car",
            max_nodes=512,
            max_snap_distance_m=100,
            timeout_seconds=30,
            max_response_bytes=32_000_000,
        )
        await incumbent(
            factory,
            region,
            provider,
            {
                lots[0]: region["vehicles"][0],
                lots[1]: region["vehicles"][0],
                lots[2]: region["vehicles"][1],
            },
        )
        async with factory() as session:
            root = await session.scalar(
                select(VehicleRoute).where(
                    VehicleRoute.vehicle_id == region["vehicles"][0],
                    VehicleRoute.status != "CLOSED",
                )
            )
            revision = await session.scalar(
                select(RouteRevision).where(
                    RouteRevision.route_id == root.route_id, RouteRevision.version == root.version
                )
            )
        original = {
            "route_id": str(root.route_id),
            "rider_id": str(region["riders"][0]),
            "vehicle_id": str(region["vehicles"][0]),
        }
        await operational(
            factory,
            region["riders"][0],
            AcknowledgeRoute(
                client_command_id=new_uuid7(),
                revision_id=revision.revision_id,
            ),
            region["clock"],
        )
        for _ in range(2):
            await progress(factory, region, original, "collect")
        original_attempts = {}
        async with factory() as session:
            assert await cargo_load(session, region["vehicles"][0]) == (2500, 2500)
            for lot in lots[:2]:
                original_attempts[lot] = (await booking_lineage(session, lot))["pickup_attempts"]
                assert (await custodian(session, lot))[1] == region["riders"][0]
        # Synthetic authoritative positions: healthy vehicles have remained at their
        # origin while the failed vehicle performed its two pickups.
        for vehicle in region["vehicles"][1:]:
            await resource(
                factory,
                region["manager"],
                PositionReport(
                    client_command_id=new_uuid7(),
                    vehicle_id=vehicle,
                    longitude=region["origin"]["longitude"],
                    latitude=region["origin"]["latitude"],
                    observed_at=region["clock"],
                ),
                region["clock"],
            )
        incident = ManagerRequestReplacement(
            client_command_id=new_uuid7(),
            request_id=lots[0],
            include_route=True,
            expected_revision_id=revision.revision_id,
            reason="VEHICLE_UNAVAILABLE",
        )
        replacement = await operational(factory, region["manager"], incident, region["clock"])
        assert await recommend_replacement(
            factory,
            replacement,
            providers={"qa-car": provider},
            optimizers={
                OptimizerEngine.VROOM: VroomOptimizer(),
                OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
            },
            now=region["clock"],
        )
        async with factory() as session:
            view = await read_replacement_options(session, replacement, region["clock"])
            choice = await session.scalar(
                select(ReplacementCandidate).where(
                    ReplacementCandidate.evaluation_id == view["evaluation"]["evaluation_id"],
                    ReplacementCandidate.shift_id == region["shifts"][1],
                )
            )
        assert choice is not None, view
        assert set(choice.summary["physical_handoff_required"]) == set(map(str, lots[:2]))
        assert str(lots[2]) in choice.summary["existing_bookings"]
        await operational(
            factory,
            region["manager"],
            DecideReplacement(
                client_command_id=new_uuid7(),
                replacement_id=replacement,
                expected_version=view["version"],
                selected_candidate_id=choice.candidate_id,
                expected_source_version=view["evaluation"]["source_version"],
                expected_region_version=view["evaluation"]["region_version"],
                decision="APPROVE",
            ),
            region["clock"],
        )
        async with factory() as session:
            group = list(
                await session.scalars(
                    select(RiderReplacement)
                    .where(RiderReplacement.incident_id == incident.client_command_id)
                    .order_by(RiderReplacement.transfer_intent_id.nulls_last())
                )
            )
            assert len(group) == 2 and all(r.status == "APPROVED" for r in group)
            for row in group:
                assert (await custodian(session, row.request_id))[1] == region["riders"][0]
        # Actual physical receipts are separate from planner witnesses and manager
        # approval. Existing custody authority records each household independently.
        for row in group:
            async with factory() as session:
                pending = await session.get(RiderReplacement, row.replacement_id)
            intent = pending.transfer_intent_id
            if intent is None:
                await operational(
                    factory,
                    region["manager"],
                    PrepareReplacementTransfer(
                        client_command_id=new_uuid7(),
                        replacement_id=pending.replacement_id,
                        expected_version=pending.version,
                    ),
                    region["clock"],
                )
                async with factory() as session:
                    pending = await session.get(RiderReplacement, row.replacement_id)
                    intent = pending.transfer_intent_id
            complete = CompleteTransfer(client_command_id=new_uuid7(), intent_id=intent)
            with pytest.raises(RouteAuthorityError, match="RECEIPTS"):
                await operational(factory, region["manager"], complete, region["clock"])
            for rider in (region["riders"][0], region["riders"][1]):
                await operational(
                    factory,
                    rider,
                    TransferReceipt(
                        client_command_id=new_uuid7(),
                        intent_id=intent,
                        scanned_request_id=row.request_id,
                        captured_at=region["clock"],
                    ),
                    region["clock"],
                )
            result = await operational(factory, region["manager"], complete, region["clock"])
            assert (
                await operational(factory, region["manager"], complete, region["clock"]) == result
            )
            async with factory() as session:
                pending = await session.get(RiderReplacement, row.replacement_id)
                assert pending.status == "APPROVED" and pending.activation_id is None
                assert (await custodian(session, row.request_id))[1] == region["riders"][1]
        region["clock"] += timedelta(seconds=1)
        cycle = await run_region(
            factory,
            region_id=region["region_id"],
            providers={"qa-car": provider},
            workers=local_workers,
            clock=lambda: region["clock"],
        )
        assert cycle.status == "ACTIVATED", cycle.failure_code
        async with factory() as session:
            assert await cargo_load(session, region["vehicles"][0]) == (0, 0)
            assert await cargo_load(session, region["vehicles"][1]) == (2500, 2500)
            for row in group:
                done = await session.get(RiderReplacement, row.replacement_id)
                assert done.status == "COMPLETED" and done.activation_id == cycle.activation_id, {
                    "replacement": (
                        done.request_id,
                        done.status,
                        done.transfer_intent_id,
                        done.failure_code,
                    ),
                    "cycle": cycle.measurements,
                    "route_tasks": [
                        (t.route_id, t.kind, t.state, t.pickup_execution_id)
                        for t in await session.scalars(select(RouteTask))
                    ],
                }
                assert (await booking_lineage(session, row.request_id))[
                    "pickup_attempts"
                ] == original_attempts[row.request_id]
        async with factory() as session:
            receiver = await session.scalar(
                select(VehicleRoute).where(
                    VehicleRoute.vehicle_id == region["vehicles"][1],
                    VehicleRoute.status == "ACTIVE",
                )
            )
            current = await session.scalar(
                select(RouteRevision).where(
                    RouteRevision.route_id == receiver.route_id,
                    RouteRevision.version == receiver.version,
                )
            )
        await operational(
            factory,
            region["riders"][1],
            AcknowledgeRoute(
                client_command_id=new_uuid7(),
                revision_id=current.revision_id,
            ),
            region["clock"],
        )
        executing = {
            "route_id": str(receiver.route_id),
            "rider_id": str(region["riders"][1]),
            "vehicle_id": str(region["vehicles"][1]),
        }
        for _ in range(3):
            await progress(factory, region, executing, "discharge")
        async with factory() as session:
            assert await cargo_load(session, region["vehicles"][1]) == (0, 0)
            for lot in lots[:2]:
                assert (await booking_lineage(session, lot))[
                    "pickup_attempts"
                ] == original_attempts[lot]
