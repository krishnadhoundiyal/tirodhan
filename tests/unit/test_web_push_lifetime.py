"""Real subprocess cancellation and stalled libc resolver isolation; no live HTTP."""

import asyncio
import concurrent.futures
import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tirodhan.modules.customer_account.web_push_transport import WebPushTransport


class UnexpectedAbort:
    def abort(self) -> None:
        raise AssertionError("normal lifecycle must finish before the fail-stop watchdog")


def stall_command(marker: Path) -> tuple[str, ...]:
    # Enter the actual production connect() path, then stall its synchronous DNS call.
    code = (
        "import socket,time; from pathlib import Path; "
        "from tirodhan.modules.customer_account.web_push_child import main; "
        f"marker=Path({str(marker)!r}); "
        "socket.getaddrinfo=lambda *a,**k: (marker.write_text('DNS'),time.sleep(3600))[1]; "
        "main()"
    )
    return (sys.executable, "-c", code)


async def entered(marker: Path) -> None:
    async def poll() -> None:
        while not marker.exists():  # noqa: ASYNC110, ASYNC240 - cross-process readiness
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), 10)


def track_children(monkeypatch: Any) -> list[Any]:
    children: list[Any] = []
    spawn = asyncio.create_subprocess_exec

    async def tracked(*args: Any, **kwargs: Any) -> Any:
        child = await spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    return children


@pytest.mark.asyncio
async def test_stalled_dns_is_killed_and_reaped_without_executor_leak(
    monkeypatch: Any, tmp_path: Path
) -> None:
    children = track_children(monkeypatch)
    transport = WebPushTransport(timeout_seconds=3, owner=UnexpectedAbort())
    marker = tmp_path / "dns"
    monkeypatch.setattr(transport, "command", lambda: stall_command(marker))
    started = time.monotonic()
    for _ in range(2):
        with pytest.raises(asyncio.TimeoutError):
            await transport.post("https://push.example.test/p", b"ciphertext", {})
        assert marker.exists(), "test must reach the blocked production DNS call"
        assert children[-1].returncode is not None
    assert len(children) == 2 and time.monotonic() - started < 12
    await transport.close()


@pytest.mark.asyncio
async def test_exhausted_executor_does_not_block_send_and_excess_calls_do_not_spawn(
    monkeypatch: Any, tmp_path: Path
) -> None:
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    occupied = executor.submit(release.wait)
    old_executor = loop._default_executor  # type: ignore[attr-defined]
    loop.set_default_executor(executor)
    children = track_children(monkeypatch)
    transport = WebPushTransport(timeout_seconds=15, owner=UnexpectedAbort())
    marker = tmp_path / "dns"
    monkeypatch.setattr(transport, "command", lambda: stall_command(marker))
    task = asyncio.create_task(transport.post("https://push.example.test/p", b"body", {}))
    try:
        await entered(marker)
        outcomes = await asyncio.gather(
            *(transport.post("https://push.example.test/p", b"body", {}) for _ in range(50)),
            return_exceptions=True,
        )
        assert all(isinstance(result, OSError) for result in outcomes)
        assert len(children) == 1
        await asyncio.wait_for(transport.close(), 3)
        assert children[0].returncode is not None
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(OSError):
            await transport.post("https://push.example.test/p", b"body", {})
    finally:
        await transport.close()
        release.set()
        occupied.result(timeout=3)
        loop._default_executor = old_executor  # type: ignore[attr-defined]
        executor.shutdown()


@pytest.mark.asyncio
async def test_cancellation_during_spawn_still_reaps_child(monkeypatch: Any) -> None:
    spawn = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    children: list[Any] = []

    async def delayed(*args: Any, **kwargs: Any) -> Any:
        child = await spawn(sys.executable, "-c", "import time;time.sleep(3600)", **kwargs)
        children.append(child)
        spawned.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    transport = WebPushTransport(owner=UnexpectedAbort())
    task = asyncio.create_task(transport.post("https://push.example.test/p", b"body", {}))
    await asyncio.wait_for(spawned.wait(), 10)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert children[0].returncode is not None


def test_asyncio_run_shutdown_reaps_stalled_dns_child(tmp_path: Path) -> None:
    marker = tmp_path / "dns"
    code = f"""
import asyncio
from pathlib import Path
from tirodhan.modules.customer_account.web_push_transport import WebPushTransport
from types import SimpleNamespace
import os
async def run():
    transport=WebPushTransport(owner=SimpleNamespace(abort=lambda: os._exit(91)))
    transport.command=lambda: {stall_command(marker)!r}
    task=asyncio.create_task(transport.post('https://push.example.test/p',b'body',{{}}))
    while not Path({str(marker)!r}).exists():
        await asyncio.sleep(.02)
    # Return with a pending send, exactly as asyncio.run shutdown cancels worker tasks.
asyncio.run(run())
print('SHUTDOWN_COMPLETE')
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=15, check=True
    )
    assert result.stdout.strip() == b"SHUTDOWN_COMPLETE"


def test_child_watchdog_exits_even_without_a_supervising_parent(tmp_path: Path) -> None:
    # Shorten only the timer in this child. The production watchdog is twenty seconds.
    command = list(stall_command(tmp_path / "dns"))
    command[-1] = command[-1].replace(
        "main()",
        "import threading; original=threading.Timer; "
        "threading.Timer=lambda seconds, callback: original(.5, callback); main()",
    )
    result = subprocess.run(
        command,
        input=b'{"endpoint":"https://push.example.test/p","body":"","headers":{}}',
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert (tmp_path / "dns").exists()
    assert result.stdout == result.stderr == b""


@pytest.mark.asyncio
async def test_child_pipe_success_uses_no_shell_or_secret_arguments(monkeypatch: Any) -> None:
    if sys.platform == "linux":

        def forbidden(*args: Any, **kwargs: Any) -> None:
            pytest.fail("the worker watchdog must not depend on GIL-held posix_spawn")

        monkeypatch.setattr(subprocess.Popen, "_posix_spawn", forbidden)
    transport = WebPushTransport(owner=UnexpectedAbort())
    command = (
        sys.executable,
        "-c",
        "import tirodhan.modules.customer_account.web_push_child as child; "
        "child.post_public=lambda endpoint,body,headers: 201; child.main()",
    )
    monkeypatch.setattr(transport, "command", lambda: command)
    assert (
        await transport.post(
            "https://push.example.test/private-token",
            b"ciphertext",
            {"Authorization": "private-authorization"},
        )
        == 201
    )
    assert all(
        "private-token" not in argument and "private-authorization" not in argument
        for argument in command
    )
    await transport.close()


@pytest.mark.skipif(
    sys.platform != "linux" or sys.version_info < (3, 12),
    reason="production ownership requires CPython 3.12+ on Linux",
)
def test_finite_worker_sigterm_cancels_and_reaps_stalled_dns(tmp_path: Path) -> None:
    marker = tmp_path / "dns"
    code = f"""
import asyncio
from tirodhan.workers import customer_notifications as worker
from tirodhan.modules.customer_account.web_push_transport import WebPushTransport
from tirodhan.modules.customer_account.web_push_ownership import ProcessGroupOwner
from types import SimpleNamespace
async def run(owner=None):
    assert isinstance(owner, ProcessGroupOwner)
    transport=WebPushTransport(owner=owner)
    transport.command=lambda: {stall_command(marker)!r}
    try:
        await transport.post('https://push.example.test/p',b'body',{{}})
    finally:
        await transport.close()
worker.run=run
worker.get_settings=lambda: SimpleNamespace(customer_web_push_enabled=True)
asyncio.run(worker.supervised_run())
print('SIGTERM_COMPLETE')
"""
    with subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as driver:
        try:
            until = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < until:
                time.sleep(0.02)
            assert marker.exists()
            driver.terminate()
            stdout, stderr = driver.communicate(timeout=3)
            assert driver.returncode == 0 and stdout.strip() == b"SIGTERM_COMPLETE"
            assert stderr == b""
        finally:
            if driver.poll() is None:
                driver.kill()
                driver.wait(timeout=3)


@pytest.mark.asyncio
async def test_missing_worker_ownership_rejects_before_launch(monkeypatch: Any) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("uncontained Web Push must not create a subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(OSError, match="ownership unavailable"):
        await WebPushTransport().post("https://push.example.test/p", b"body", {})


@pytest.mark.skipif(
    sys.platform != "linux" or sys.version_info < (3, 12),
    reason="real group kill and Linux orphan adoption on the supported worker runtime",
)
@pytest.mark.parametrize("with_child", [False, True])
@pytest.mark.parametrize("shutdown", ["timeout", "close", "cancel", "event_loop"])
def test_permanently_unresolved_spawn_has_bounded_owned_shutdown(
    tmp_path: Path, with_child: bool, shutdown: str
) -> None:
    marker = tmp_path / "ownership.json"
    # Adopt/reap the disposable worker's child after group termination, just as the
    # container runtime/init must do. Never signal the pytest runner's process group.
    libc = ctypes.CDLL(None, use_errno=True)
    old_subreaper = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(old_subreaper), 0, 0, 0) == 0  # GET_CHILD_SUBREAPER
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # SET_CHILD_SUBREAPER
    code = f"""
import asyncio,json,os,sys
from pathlib import Path
from tirodhan.modules.customer_account.web_push_transport import WebPushTransport
from tirodhan.modules.customer_account.web_push_ownership import ProcessGroupOwner
async def run():
    owner=ProcessGroupOwner()
    port=WebPushTransport(timeout_seconds=.5,cleanup_seconds=.3,owner=owner)
    original=asyncio.create_subprocess_exec
    entered=asyncio.Event()
    async def never_resolves(*args,**kwargs):
        child=None
        if {with_child!r}:
            child=await original(sys.executable,'-c','import time;time.sleep(3600)',**kwargs)
            assert os.getpgid(child.pid)==os.getpgrp()
        Path({str(marker)!r}).write_text(json.dumps({{
            'child':child.pid if child else None,'group':os.getpgrp()
        }}))
        entered.set()
        # Never release launch, including during asyncio.run's final cancellation sweep.
        while True:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                pass
    asyncio.create_subprocess_exec=never_resolves
    task=asyncio.create_task(port.post('https://push.example.test/p',b'body',{{}}))
    await entered.wait()
    if {shutdown!r}=='close':
        await port.close()
    elif {shutdown!r}=='cancel':
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await task
    elif {shutdown!r}=='event_loop':
        return
    else:
        await task
asyncio.run(run())
print('UNSAFE_RETURN')
"""
    child_pid = None
    reaped = False
    try:
        with subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as driver:
            started = time.monotonic()
            try:
                stdout, stderr = driver.communicate(timeout=5)
                assert driver.returncode == -signal.SIGKILL
                assert time.monotonic() - started < 3
                assert stdout == stderr == b""
                ownership = json.loads(marker.read_text())
                assert ownership["group"] == driver.pid
                child_pid = ownership["child"]
                assert (child_pid is not None) == with_child
            finally:
                if driver.poll() is None:
                    os.killpg(driver.pid, signal.SIGKILL)
                    driver.wait(timeout=3)
                if marker.exists():
                    child_pid = json.loads(marker.read_text())["child"]
        if child_pid is not None:
            until = time.monotonic() + 2
            while time.monotonic() < until:
                pid, status = os.waitpid(child_pid, os.WNOHANG)
                if pid:
                    reaped = True
                    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
                    break
                time.sleep(0.01)
            assert reaped, "the potentially created child must be killed and reaped"
    finally:
        if child_pid is not None and not reaped:
            try:
                os.kill(child_pid, signal.SIGKILL)
                os.waitpid(child_pid, 0)
            except ProcessLookupError:
                pass
        assert libc.prctl(36, old_subreaper.value, 0, 0, 0) == 0
