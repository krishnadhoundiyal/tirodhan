import asyncio
import sys
from dataclasses import replace

import pytest

from tirodhan.modules.planning.routing import execution
from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import TimeWindow
from tirodhan.modules.planning.routing.execution import (
    RoutingAdapterError,
    decode_object,
    run_native,
)


async def child(code, **kwargs):
    return await run_native(
        (sys.executable, "-c", code),
        None,
        timeout_seconds=kwargs.get("timeout", 5),
        max_output_bytes=kwargs.get("limit", 1000),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_kill_and_reap_real_process(monkeypatch, cancel):
    original = asyncio.create_subprocess_exec
    created = asyncio.Event()
    processes = []

    async def observe(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        created.set()
        return process

    monkeypatch.setattr(execution.asyncio, "create_subprocess_exec", observe)
    task = asyncio.create_task(child("import time; time.sleep(60)", timeout=0.3))
    await asyncio.wait_for(created.wait(), 5)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RoutingAdapterError, match="SOLVER_TIMEOUT"):
            await task
    assert processes[0].returncode is not None
    assert (await child("print('recovered')")).strip() == b"recovered"


@pytest.mark.asyncio
async def test_crash_output_bounds_and_missing_binary_are_controlled():
    with pytest.raises(RoutingAdapterError, match="SOLVER_FAILED") as error:
        await child("import sys; print('private-coordinates', file=sys.stderr); sys.exit(3)")
    assert str(error.value) == "SOLVER_FAILED"
    with pytest.raises(RoutingAdapterError, match="RESPONSE_TOO_LARGE"):
        await child("print('x' * 10000)", limit=100)
    with pytest.raises(RoutingAdapterError, match="SOLVER_UNAVAILABLE"):
        await run_native(
            ("nonexistent-tirodhan-routing-binary",),
            None,
            timeout_seconds=1,
            max_output_bytes=100,
        )
    assert (await child("print('recovered')")).strip() == b"recovered"


@pytest.mark.parametrize("raw", [b"[]", b'{"routes":[],"routes":[]}', b"not JSON"])
def test_native_invalid_json_is_controlled(raw):
    with pytest.raises(RoutingAdapterError, match="INVALID_RESPONSE"):
        decode_object(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", [VroomOptimizer, ORToolsOptimizer])
async def test_independent_gate_rejects_native_success_with_missing_required_stops(
    monkeypatch, routing_problem, engine
):
    async def native(command, *args, **kwargs):
        if command[-1] == "-v":
            return b"vroom 1.15.0\n"
        return b'{"code":0,"routes":[],"summary":{"cost":0},"objective":0}'

    monkeypatch.setattr("tirodhan.modules.planning.routing.adapters.run_native", native)
    with pytest.raises(RoutingAdapterError, match="NO_FEASIBLE_PROPOSAL"):
        await engine().propose(routing_problem, search_seconds=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", [VroomOptimizer, ORToolsOptimizer])
async def test_objective_is_checked_independently(monkeypatch, routing_problem, engine):
    async def native(command, *args, **kwargs):
        if command[-1] == "-v":
            return b"vroom 1.15.0\n"
        return (
            b'{"code":0,"routes":[{"vehicle":1,"jobs":[2,3,4],"steps":'
            b'[{"type":"pickup","id":2},{"type":"job","id":3},'
            b'{"type":"delivery","id":4}]}],"summary":{"cost":4294967295},'
            b'"objective":4294967295}'
        )

    monkeypatch.setattr("tirodhan.modules.planning.routing.adapters.run_native", native)
    with pytest.raises(RoutingAdapterError, match="OBJECTIVE_MISMATCH"):
        await engine().propose(routing_problem, search_seconds=1)


@pytest.mark.asyncio
async def test_version_mismatch_and_unsupported_range_fail_closed(monkeypatch, routing_problem):
    async def wrong_version(*args, **kwargs):
        return b"vroom 1.14.0\n"

    monkeypatch.setattr("tirodhan.modules.planning.routing.adapters.run_native", wrong_version)
    with pytest.raises(RoutingAdapterError, match="SOLVER_VERSION_MISMATCH"):
        await VroomOptimizer().propose(routing_problem, search_seconds=1)
    vehicle = replace(routing_problem.vehicles[0], shift=TimeWindow(0, 2**31))
    with pytest.raises(RoutingAdapterError, match="NATIVE_RANGE_EXCEEDED"):
        await ORToolsOptimizer().propose(
            replace(routing_problem, vehicles=(vehicle,)), search_seconds=1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [True, 0, 31, 1.5])
async def test_search_budget_is_bounded_before_execution(routing_problem, budget):
    with pytest.raises(RoutingAdapterError, match="INVALID_SEARCH_BUDGET"):
        await ORToolsOptimizer().propose(routing_problem, search_seconds=budget)
