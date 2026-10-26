"""Real subprocess cancellation and stalled libc resolver isolation; no live HTTP."""

import asyncio
import concurrent.futures
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tirodhan.modules.customer_account.web_push_transport import WebPushTransport


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
    transport = WebPushTransport(timeout_seconds=3)
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
    transport = WebPushTransport(timeout_seconds=15)
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
    transport = WebPushTransport()
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
async def run():
    transport=WebPushTransport()
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
    transport = WebPushTransport()
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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows terminate is not POSIX SIGTERM")
def test_finite_worker_sigterm_cancels_and_reaps_stalled_dns(tmp_path: Path) -> None:
    marker = tmp_path / "dns"
    code = f"""
import asyncio
from tirodhan.workers import customer_notifications as worker
from tirodhan.modules.customer_account.web_push_transport import WebPushTransport
async def run():
    transport=WebPushTransport()
    transport.command=lambda: {stall_command(marker)!r}
    try:
        await transport.post('https://push.example.test/p',b'body',{{}})
    finally:
        await transport.close()
worker.run=run
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
