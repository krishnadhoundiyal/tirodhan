"""A single killable Web Push child; DNS never uses the parent's executor."""

import asyncio
import base64
import json
import sys


class WebPushTransport:
    # Admission is per finite delivery port. Excess callers retry through durable delivery debt.
    # No waiting queue, executor tasks, persistent resolver or additional service is created.
    def __init__(self, timeout_seconds: float = 20) -> None:
        self.timeout_seconds = timeout_seconds
        self._active: asyncio.Task[int] | None = None
        self._closed = False

    def command(self) -> tuple[str, ...]:
        return (sys.executable, "-m", "tirodhan.modules.customer_account.web_push_child")

    async def post(self, endpoint: str, body: bytes, headers: dict[str, str]) -> int:
        if self._closed or self._active is not None:
            raise OSError("push transport unavailable")
        payload = json.dumps(
            {"endpoint": endpoint, "body": base64.b64encode(body).decode(), "headers": headers}
        ).encode()
        if len(payload) > 16384:
            raise OSError("push request too large")
        task = asyncio.create_task(self._run(payload))
        self._active = task
        try:
            return await task
        finally:
            self._active = None

    async def _run(self, payload: bytes) -> int:
        # Shield creation: cancellation during spawn must still acquire and reap the child.
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *self.command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        process = None
        try:

            async def exchange() -> int:
                nonlocal process
                process = await asyncio.shield(spawn)
                stdout, _ = await process.communicate(payload)
                if process.returncode != 0 or len(stdout) != 3 or not stdout.isdigit():
                    raise OSError("push child failed")
                return int(stdout)

            return await asyncio.wait_for(exchange(), self.timeout_seconds)
        finally:

            async def reap() -> None:
                child = process if process is not None else await spawn
                if child.returncode is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                # Drain/close pipe transports too; wait() alone can leave Windows IOCP
                # callbacks alive past event-loop shutdown after communicate cancellation.
                await child.communicate()

            cleanup = asyncio.create_task(reap())
            # Keep cleanup alive if close() and caller cancellation arrive together.
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    async def close(self) -> None:
        self._closed = True
        task = self._active
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
