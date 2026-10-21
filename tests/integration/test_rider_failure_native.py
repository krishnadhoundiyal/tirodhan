"""Real OSRM, both native repair fallbacks and manager-approved multi-rider activation."""

import os
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from geographical_execution import progress
from geographical_fixture import DATA, arrivals, operational, resource, setup_region
from sqlalchemy import select
from test_geographical_rolling import ENABLED, geographical_requirements  # noqa: F401
from test_manager_replacement_native import incumbent

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import OptimizerEngine
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.planning.routing.validation import RoutingValidationError
from tirodhan.modules.routing_operations.authority import RouteAuthorityError
from tirodhan.modules.routing_operations.capacity_models import RiderReplacement
from tirodhan.modules.routing_operations.commands import PositionReport
from tirodhan.modules.routing_operations.dispatch_models import RouteRevision, VehicleRoute
from tirodhan.modules.routing_operations.failure_commands import (
    ApproveRecovery,
    FailRider,
    RecoverySelection,
    RetryRecovery,
    execute_failure,
)
from tirodhan.modules.routing_operations.failure_execution import activate_recovery
from tirodhan.modules.routing_operations.failure_models import (
    RecoveryAssignment,
    RiderFailureIncident,
)
from tirodhan.modules.routing_operations.failure_runtime import recommend_one
from tirodhan.modules.routing_operations.operational_commands import (
    AbortTransfer,
    AcknowledgeRoute,
    CompleteTransfer,
    TransferReceipt,
    custodian,
)
from tirodhan.modules.routing_operations.replacement_models import ReplacementCandidate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.routing_engine,
    pytest.mark.asyncio,
    pytest.mark.skipif(not ENABLED, reason="explicit native recovery opt-in"),
]


@pytest.mark.parametrize("native", [OptimizerEngine.VROOM, OptimizerEngine.OR_TOOLS])
async def test_native_multi_rider_recovery_and_individual_custody(
    database_session_factory, monkeypatch, native
):
    factory = database_session_factory
    nodes = DATA["regions"]["North"]["nodes"]
    region = await setup_region(
        factory,
        "North",
        4,
        7200,
        public_nodes=nodes[:240] + [nodes[-4]] * 4,
        shift_end_seconds={0: 7200},
    )
    records = await arrivals(factory, region, 2, 1)
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
        await incumbent(factory, region, provider, {lot: region["vehicles"][0] for lot in lots})
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
        # Both lots are physically collected. Expiry retains custody and removes supply.
        await operational(
            factory,
            region["riders"][0],
            AcknowledgeRoute(client_command_id=new_uuid7(), revision_id=revision.revision_id),
            region["clock"],
        )
        await progress(
            factory,
            region,
            {
                "route_id": str(root.route_id),
                "rider_id": str(region["riders"][0]),
                "vehicle_id": str(region["vehicles"][0]),
            },
            "collect",
        )
        await progress(
            factory,
            region,
            {
                "route_id": str(root.route_id),
                "rider_id": str(region["riders"][0]),
                "vehicle_id": str(region["vehicles"][0]),
            },
            "collect",
        )
        from tirodhan.modules.routing_operations.models import OperatingShift
        from tirodhan.modules.routing_operations.recovery_planning import donor_positions

        async with factory() as session:
            donor_shift = await session.get(OperatingShift, region["shifts"][0])
            positions = await donor_positions(session, set(region["vehicles"]))
        region["clock"] = max(region["clock"], donor_shift.ends_at + timedelta(seconds=1))
        for vehicle in region["vehicles"]:
            position = positions[str(vehicle)]
            await resource(
                factory,
                region["manager"],
                PositionReport(
                    client_command_id=new_uuid7(),
                    vehicle_id=vehicle,
                    longitude=position["longitude"],
                    latitude=position["latitude"],
                    observed_at=region["clock"],
                ),
                region["clock"],
            )
        incident = await execute_failure(
            factory,
            actor=region["manager"],
            command=FailRider(
                client_command_id=new_uuid7(),
                rider_id=region["riders"][0],
                reason="VEHICLE_UNAVAILABLE",
                expected_route_versions={root.route_id: root.version},
            ),
            now=region["clock"],
        )
        optimizers = {
            OptimizerEngine.VROOM: VroomOptimizer(),
            OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
        }
        async with factory() as session:
            rows = list(
                await session.scalars(
                    select(RiderReplacement)
                    .where(RiderReplacement.failure_incident_id == incident)
                    .order_by(RiderReplacement.request_id)
                )
            )
        assert len(rows) == 2
        choices = []
        for index, row in enumerate(rows, 1):
            assert await recommend_one(
                factory,
                row.replacement_id,
                providers={"qa-car": provider},
                optimizers=optimizers,
                now=region["clock"],
            )
            async with factory() as session:
                current = await session.get(RiderReplacement, row.replacement_id)
                option = await session.scalar(
                    select(ReplacementCandidate).where(
                        ReplacementCandidate.evaluation_id == current.evaluation_id,
                        ReplacementCandidate.shift_id == region["shifts"][index],
                    )
                )
            assert option
            choices.append(
                RecoverySelection(
                    candidate_id=option.candidate_id, replacement_ids=[row.replacement_id]
                )
            )
        async with factory() as session:
            parent = await session.get(RiderFailureIncident, incident)
        await execute_failure(
            factory,
            actor=region["manager"],
            command=ApproveRecovery(
                client_command_id=new_uuid7(),
                incident_id=incident,
                expected_version=parent.version,
                selections=choices,
                reason="MULTI_RIDER_RESCUE",
            ),
            now=region["clock"],
        )
        async with factory() as session:
            legs = list(
                await session.scalars(
                    select(RecoveryAssignment)
                    .where(RecoveryAssignment.incident_id == incident)
                    .order_by(RecoveryAssignment.shift_id)
                )
            )
        assert len({leg.shift_id for leg in legs}) == 2
        # Explicitly exercise each real native fallback. The fast insertion path is
        # separately covered; no fabricated native result is used here.
        from tirodhan.modules.routing_operations import failure_execution

        def bounded_out(*args, **kwargs):
            raise RoutingValidationError("FORCED_LOCAL_SEARCH_BUDGET")

        monkeypatch.setattr(failure_execution, "repair", bounded_out)

        class Unavailable:
            async def propose(self, *args, **kwargs):
                raise RoutingAdapterError("TEST_OTHER_ENGINE_UNAVAILABLE")

        selected = {
            engine: optimizer if engine == native else Unavailable()
            for engine, optimizer in optimizers.items()
        }
        for index, leg in enumerate(legs):
            result = await activate_recovery(
                factory,
                leg.assignment_id,
                providers={"qa-car": provider},
                optimizers=selected,
                now=region["clock"],
            )
            if result is None:
                async with factory() as session:
                    current = await session.get(RecoveryAssignment, leg.assignment_id)
                    row = await session.scalar(
                        select(RiderReplacement).where(
                            RiderReplacement.recommended_shift_id == leg.shift_id,
                            RiderReplacement.failure_incident_id == incident,
                        )
                    )
                assert current.status == "AWAITING_TRANSFER", current.failure_code
                async with factory() as session:
                    assert (await custodian(session, row.request_id))[1] == region["riders"][0]
                if index == 0:
                    # Unresolved transfer cannot be withdrawn. An evidenced abort
                    # must allow an explicit fresh manager decision, preserving history.
                    blocked_retry = RetryRecovery(
                        client_command_id=new_uuid7(),
                        assignment_id=leg.assignment_id,
                        expected_version=current.version,
                        request_new_options=True,
                    )
                    with pytest.raises(RouteAuthorityError, match="RESOLVE_PHYSICAL_TRANSFER"):
                        await execute_failure(
                            factory,
                            actor=region["manager"],
                            command=blocked_retry,
                            now=region["clock"],
                        )
                    aborted = row.transfer_intent_id
                    await operational(
                        factory,
                        region["manager"],
                        AbortTransfer(
                            client_command_id=new_uuid7(),
                            intent_id=aborted,
                            scanned_request_id=row.request_id,
                            client_capture_id=new_uuid7(),
                            captured_at=region["clock"],
                            material_with_current_custodian=True,
                        ),
                        region["clock"],
                    )
                    region["clock"] += timedelta(seconds=11)
                    assert (
                        await activate_recovery(
                            factory,
                            leg.assignment_id,
                            providers={"qa-car": provider},
                            optimizers=selected,
                            now=region["clock"],
                        )
                        is None
                    )
                    async with factory() as session:
                        current = await session.get(RecoveryAssignment, leg.assignment_id)
                    assert current.status == "ATTENTION", current.failure_code
                    retry = RetryRecovery(
                        client_command_id=new_uuid7(),
                        assignment_id=leg.assignment_id,
                        expected_version=current.version,
                        request_new_options=(native == OptimizerEngine.OR_TOOLS),
                    )
                    await execute_failure(
                        factory, actor=region["manager"], command=retry, now=region["clock"]
                    )
                    assert (
                        await execute_failure(
                            factory, actor=region["manager"], command=retry, now=region["clock"]
                        )
                        == leg.assignment_id
                    )
                    if retry.request_new_options:
                        assert await recommend_one(
                            factory,
                            row.replacement_id,
                            providers={"qa-car": provider},
                            optimizers=optimizers,
                            now=region["clock"],
                        )
                        async with factory() as session:
                            row = await session.get(RiderReplacement, row.replacement_id)
                            option = await session.scalar(
                                select(ReplacementCandidate).where(
                                    ReplacementCandidate.evaluation_id == row.evaluation_id,
                                    ReplacementCandidate.shift_id == leg.shift_id,
                                )
                            )
                            parent = await session.get(RiderFailureIncident, incident)
                        assert option
                        await execute_failure(
                            factory,
                            actor=region["manager"],
                            command=ApproveRecovery(
                                client_command_id=new_uuid7(),
                                incident_id=incident,
                                expected_version=parent.version,
                                reason="ABORTED_RENDEZVOUS_RETRY",
                                selections=[
                                    RecoverySelection(
                                        candidate_id=option.candidate_id,
                                        replacement_ids=[row.replacement_id],
                                    )
                                ],
                            ),
                            now=region["clock"],
                        )
                        async with factory() as session:
                            leg = await session.scalar(
                                select(RecoveryAssignment).where(
                                    RecoveryAssignment.incident_id == incident,
                                    RecoveryAssignment.shift_id == leg.shift_id,
                                    RecoveryAssignment.status == "PENDING",
                                )
                            )
                    assert (
                        await activate_recovery(
                            factory,
                            leg.assignment_id,
                            providers={"qa-car": provider},
                            optimizers=selected,
                            now=region["clock"],
                        )
                        is None
                    )
                    from tirodhan.modules.routing_operations.orchestration_models import (
                        CargoTransferIntent,
                    )

                    async with factory() as session:
                        row = await session.get(RiderReplacement, row.replacement_id)
                        old_intent = await session.get(CargoTransferIntent, aborted)
                        assert old_intent.resolution == "ABORTED"
                        assert old_intent.resolution_evidence_id
                        assert row.transfer_intent_id != aborted
                        assert (await custodian(session, row.request_id))[1] == region["riders"][0]
                target = region["riders"][region["shifts"].index(leg.shift_id)]
                for rider in (region["riders"][0], target):
                    await operational(
                        factory,
                        rider,
                        TransferReceipt(
                            client_command_id=new_uuid7(),
                            intent_id=row.transfer_intent_id,
                            scanned_request_id=row.request_id,
                            captured_at=region["clock"],
                        ),
                        region["clock"],
                    )
                await operational(
                    factory,
                    region["manager"],
                    CompleteTransfer(
                        client_command_id=new_uuid7(), intent_id=row.transfer_intent_id
                    ),
                    region["clock"],
                )
                async with factory() as session:
                    due = (await session.get(RecoveryAssignment, leg.assignment_id)).due_at
                region["clock"] = max(region["clock"], due) + timedelta(seconds=1)
                result = await activate_recovery(
                    factory,
                    leg.assignment_id,
                    providers={"qa-car": provider},
                    optimizers=selected,
                    now=region["clock"],
                )
            async with factory() as session:
                current = await session.get(RecoveryAssignment, leg.assignment_id)
                assert result and current.status == "ACTIVATED", current.failure_code
                revision = await session.scalar(
                    select(RouteRevision).where(RouteRevision.activation_id == result)
                )
                root = await session.get(VehicleRoute, revision.route_id)
            rider = region["riders"][region["shifts"].index(leg.shift_id)]
            await operational(
                factory,
                rider,
                AcknowledgeRoute(client_command_id=new_uuid7(), revision_id=revision.revision_id),
                region["clock"],
            )
            route = {
                "route_id": str(root.route_id),
                "rider_id": str(rider),
                "vehicle_id": str(root.vehicle_id),
            }
            async with factory() as session:
                from tirodhan.modules.routing_operations.execution import current_tasks

                tasks = await current_tasks(session, root)
            if tasks[0].kind == "PICKUP":
                await progress(factory, region, route, "collect")
            await progress(factory, region, route, "discharge")
        async with factory() as session:
            assert all(
                r.status == "COMPLETED"
                for r in await session.scalars(
                    select(RiderReplacement).where(RiderReplacement.failure_incident_id == incident)
                )
            )

        from tirodhan.modules.routing_operations.recovery import RecoveryCommand, recover_route

        async with factory() as session:
            original_root = await session.scalar(
                select(VehicleRoute).where(VehicleRoute.vehicle_id == region["vehicles"][0])
            )
            original_revision = await session.scalar(
                select(RouteRevision).where(
                    RouteRevision.route_id == original_root.route_id,
                    RouteRevision.version == original_root.version,
                )
            )
        if original_root.status != "CLOSED":
            await recover_route(
                factory,
                manager_id=region["manager"],
                command=RecoveryCommand(
                    client_command_id=new_uuid7(),
                    route_id=original_root.route_id,
                    revision_id=original_revision.revision_id,
                    reason_code="RIDER_UNAVAILABLE",
                ),
                expires_at=region["clock"] + timedelta(days=1),
                now=region["clock"],
            )
        from tirodhan.modules.dispatch.service import RiderNotEligibleError, _lock_eligible_rider

        async with factory() as session, session.begin():
            with pytest.raises(RiderNotEligibleError, match="operational failure"):
                await _lock_eligible_rider(session, region["riders"][0])
