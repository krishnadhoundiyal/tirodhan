"""Failure/retry and eligibility tests use actual domain commands and PostgreSQL authority."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_operational_orchestration import operational
from test_regional_parallel import prepared, run
from test_routing_fleet import LineMatrix, engines

from tirodhan.db.values import new_uuid7
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.routing_operations.authority import RouteAuthorityError
from tirodhan.modules.routing_operations.capacity_models import RiderReplacement
from tirodhan.modules.routing_operations.commands import RestrictFacility
from tirodhan.modules.routing_operations.replacement_commands import (
    DecideReplacement,
    RecommendReplacement,
    RequestReplacement,
)
from tirodhan.modules.routing_operations.replacement_reads import read_replacement_options
from tirodhan.modules.routing_operations.replacements import recommend_replacement

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_pending_approval_can_select_fresh_evaluation_without_losing_old_constraint(
    database_session_factory,
):
    factory = database_session_factory
    data, identity, item, _ = await assigned_replacement(factory)

    async def evaluate():
        assert await recommend_replacement(
            factory,
            identity,
            providers={"test-car": LineMatrix()},
            optimizers=engines(),
            now=data[6][0],
        )
        async with factory() as session:
            return await read_replacement_options(session, identity, data[6][0])

    async def choose(view):
        command = DecideReplacement(
            client_command_id=new_uuid7(),
            replacement_id=identity,
            expected_version=view["version"],
            selected_candidate_id=view["candidates"][0]["candidate_id"],
            decision="APPROVE",
            expected_source_version=view["evaluation"]["source_version"],
            expected_region_version=view["evaluation"]["region_version"],
        )
        result = await operational(factory, data[0].manager_id, command, data[6][0])
        assert await operational(factory, data[0].manager_id, command, data[6][0]) == result
        return result

    view = await evaluate()
    await choose(view)
    async with factory() as session:
        approved = await session.get(RiderReplacement, identity)
        old_target = approved.recommended_shift_id
        released = (
            await session.get(RiderAssignmentItem, (item.assignment_id, item.pickup_execution_id))
        ).released_at
    await operational(
        factory,
        data[0].manager_id,
        RecommendReplacement(
            client_command_id=new_uuid7(),
            replacement_id=identity,
            expected_version=approved.version,
        ),
        data[6][0],
    )
    fresh = await evaluate()
    assert fresh["status"] == "APPROVED" and fresh["recommended_shift_id"] == old_target
    assert fresh["selection_refresh_available"] and fresh["candidates"][0]["selectable"]
    await choose(fresh)
    async with factory() as session:
        assert (
            await session.get(RiderAssignmentItem, (item.assignment_id, item.pickup_execution_id))
        ).released_at == released
    data[6][0] += timedelta(seconds=1)
    assert (await run(factory, data)).status == "ACTIVATED"
    async with factory() as session:
        assert (await session.get(RiderReplacement, identity)).status == "COMPLETED"


async def assigned_replacement(factory):
    data, _ = await prepared(factory)
    assert (await run(factory, data)).status == "ACTIVATED"
    async with factory() as session:
        item = await session.scalar(
            select(RiderAssignmentItem).where(RiderAssignmentItem.released_at.is_(None))
        )
        assignment = await session.get(RiderAssignment, item.assignment_id)
        pickup = await session.get(PickupExecution, item.pickup_execution_id)
    command = RequestReplacement(client_command_id=new_uuid7(), request_id=pickup.request_id)
    identity = await operational(factory, assignment.rider_id, command, data[6][0])
    return data, identity, item, assignment


async def test_started_household_cannot_be_interrupted_or_released(database_session_factory):
    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.routing_operations.dispatch_models import (
        RouteRevision,
        RouteTask,
        VehicleRoute,
    )
    from tirodhan.modules.routing_operations.execution import StartTask, current_tasks, execute_task
    from tirodhan.modules.routing_operations.operational_commands import AcknowledgeRoute
    from tirodhan.modules.routing_operations.replacement_commands import ManagerRequestReplacement

    factory = database_session_factory
    data, _ = await prepared(factory)
    assert (await run(factory, data)).status == "ACTIVATED"
    async with factory() as session:
        root = await session.scalar(select(VehicleRoute).where(VehicleRoute.status == "ACTIVE"))
        revision = await session.scalar(
            select(RouteRevision).where(
                RouteRevision.route_id == root.route_id, RouteRevision.version == root.version
            )
        )
        assignment = await session.get(RiderAssignment, root.assignment_id)
        task = (await current_tasks(session, root))[0]
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        booking = await session.get(CollectionRequest, pickup.request_id)
    at = booking.slot_start
    await operational(
        factory,
        assignment.rider_id,
        AcknowledgeRoute(
            client_command_id=new_uuid7(),
            revision_id=revision.revision_id,
        ),
        data[6][0],
    )
    await execute_task(
        factory,
        rider_id=assignment.rider_id,
        command=StartTask(
            client_command_id=new_uuid7(),
            task_id=task.task_id,
            revision_id=revision.revision_id,
        ),
        expires_at=at + timedelta(days=1),
        now=at,
    )
    with pytest.raises(RouteAuthorityError, match="FINISH_ACTIVE_SERVICE"):
        await operational(
            factory,
            data[0].manager_id,
            ManagerRequestReplacement(
                client_command_id=new_uuid7(),
                request_id=pickup.request_id,
                reason="VEHICLE_UNAVAILABLE",
                include_route=True,
                expected_revision_id=revision.revision_id,
            ),
            at,
        )
    identity = await operational(
        factory,
        assignment.rider_id,
        RequestReplacement(
            client_command_id=new_uuid7(),
            request_id=pickup.request_id,
        ),
        at,
    )
    assert not await recommend_replacement(
        factory, identity, providers={"test-car": LineMatrix()}, optimizers=engines(), now=at
    )
    async with factory() as session:
        assert (await session.get(RouteTask, task.task_id)).state == "STARTED"
        assert (await session.get(VehicleRoute, root.route_id)).status == "ACTIVE"
        assert (
            await session.get(RiderAssignmentItem, (root.assignment_id, pickup.pickup_execution_id))
        ).released_at is None


@pytest.mark.parametrize("failure", ["provider_timeout", "solver_timeout", "invalid_proposal"])
async def test_incomplete_search_is_unknown_preserves_ownership_and_recovers(
    database_session_factory,
    failure,
):
    factory = database_session_factory
    data, identity, item, assignment = await assigned_replacement(factory)

    class HangingProvider:
        async def acquire(self, nodes):
            await asyncio.Event().wait()

    class FailingOptimizer:
        def __init__(self, actual):
            self.actual = actual

        async def propose(self, problem, *, search_seconds):
            if failure == "solver_timeout":
                await asyncio.Event().wait()
            proposal = await self.actual.propose(problem, search_seconds=search_seconds)
            # Claim all required work unserved: the independent gate must reject it.
            return replace(
                proposal, routes=(), unserved_stop_ids=tuple(s.stop_id for s in problem.stops)
            )

    providers = {"test-car": HangingProvider() if failure == "provider_timeout" else LineMatrix()}
    optimizers = (
        engines()
        if failure == "provider_timeout"
        else {e: FailingOptimizer(o) for e, o in engines().items()}
    )
    assert not await recommend_replacement(
        factory,
        identity,
        providers=providers,
        optimizers=optimizers,
        now=data[6][0],
        max_wall_seconds=0.2,
    )
    async with factory() as session:
        row = await session.get(RiderReplacement, identity)
        view = await read_replacement_options(session, identity, data[6][0])
        retry_at = row.next_attempt_at
        assert view["evaluation"]["outcome"] == "UNKNOWN" and not view["candidates"]
        assert row.status == "REQUESTED" and row.attempt == 1
        assert (
            await session.get(RiderAssignmentItem, (item.assignment_id, item.pickup_execution_id))
        ).released_at is None
    assert await recommend_replacement(
        factory, identity, providers={"test-car": LineMatrix()}, optimizers=engines(), now=retry_at
    )


@pytest.mark.parametrize(
    "conflict", ["role_revoked", "shift_ended", "facility_closed", "unaccepted_flexible"]
)
async def test_live_conflicts_never_publish_unsafe_candidates(database_session_factory, conflict):
    from test_manager_business import change

    from tirodhan.modules.business_admin.commands import RiderRoleEdit
    from tirodhan.modules.business_admin.history import current_version
    from tirodhan.modules.identity.models import UserRole

    factory = database_session_factory
    data, identity, item, assignment = await assigned_replacement(factory)
    at = data[6][0]
    target = next(r for r in data[0].rider_ids if r != assignment.rider_id)
    if conflict == "role_revoked":
        async with factory() as session:
            role = await session.scalar(
                select(UserRole).where(UserRole.user_id == target, UserRole.role_code == "RIDER")
            )
            version = await current_version(session, "user_role", role.user_role_id)
        await change(
            factory,
            data[0].manager_id,
            RiderRoleEdit(
                client_command_id=new_uuid7(),
                resource_id=role.user_role_id,
                expected_version=version,
                reason="ELIGIBILITY_REVOKED",
                rider_id=target,
                active=False,
            ),
        )
    elif conflict == "shift_ended":
        at += timedelta(hours=12)
    elif conflict == "unaccepted_flexible":
        from tirodhan.modules.routing_operations.commands import ShiftClose, ShiftOpen
        from tirodhan.modules.routing_operations.models import OperatingShift

        async with factory() as session:
            shift = await session.scalar(
                select(OperatingShift).where(
                    OperatingShift.rider_id == target, OperatingShift.closed_at.is_(None)
                )
            )
        await data[5](ShiftClose(client_command_id=new_uuid7(), shift_id=shift.shift_id))
        await data[5](
            ShiftOpen(
                client_command_id=new_uuid7(),
                rider_id=target,
                vehicle_id=shift.vehicle_id,
                resource_kind="FLEXIBLE",
                commitment_id=new_uuid7(),
                starts_at=at - timedelta(seconds=1),
                ends_at=shift.ends_at,
            )
        )
    else:
        await data[5](
            RestrictFacility(
                client_command_id=new_uuid7(),
                receiving_point_id=data[2].receiving_point_id,
                starts_at=at,
                ends_at=at + timedelta(days=1),
                reason="CLOSED",
            )
        )
    assert not await recommend_replacement(
        factory, identity, providers={"test-car": LineMatrix()}, optimizers=engines(), now=at
    )
    async with factory() as session:
        view = await read_replacement_options(session, identity, at)
        assert view["evaluation"]["outcome"] == "UNKNOWN" and not view["candidates"]
        row = await session.get(RiderAssignmentItem, (item.assignment_id, item.pickup_execution_id))
        assert row.released_at is None
    with pytest.raises(RouteAuthorityError, match="CANDIDATE_REQUIRED"):
        await operational(
            factory,
            data[0].manager_id,
            DecideReplacement(
                client_command_id=new_uuid7(),
                replacement_id=identity,
                expected_version=view["version"],
                decision="APPROVE",
                selected_candidate_id=new_uuid7(),
            ),
            at,
        )
