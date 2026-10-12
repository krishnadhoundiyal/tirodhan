from __future__ import annotations

import asyncio
import json
from typing import Any


class RoutingAdapterError(RuntimeError):
    """Controlled codes; never expose native stderr, HTTP URLs or payloads."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def decode_object(raw: bytes) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs)
        if not isinstance(value, dict):
            raise ValueError("not an object")
    except (ValueError, UnicodeError, RecursionError):
        raise RoutingAdapterError("INVALID_RESPONSE") from None
    return value


async def run_native(
    command: tuple[str, ...],
    payload: dict[str, Any] | None,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> bytes:
    """No shell, private stdin, bounded stdout; kill/reap on timeout or cancellation."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise RoutingAdapterError("SOLVER_UNAVAILABLE") from None

    async def exchange() -> bytes:
        assert process.stdin is not None and process.stdout is not None
        data = b"" if payload is None else json.dumps(payload, allow_nan=False).encode()
        process.stdin.write(data)
        await process.stdin.drain()
        process.stdin.close()
        chunks: list[bytes] = []
        size = 0
        while chunk := await process.stdout.read(min(65536, max_output_bytes + 1)):
            size += len(chunk)
            if size > max_output_bytes:
                raise RoutingAdapterError("RESPONSE_TOO_LARGE")
            chunks.append(chunk)
        await process.wait()
        if process.returncode != 0:
            raise RoutingAdapterError("SOLVER_FAILED")
        return b"".join(chunks)

    try:
        return await asyncio.wait_for(exchange(), timeout_seconds)
    except asyncio.TimeoutError:
        raise RoutingAdapterError("SOLVER_TIMEOUT") from None
    except (BrokenPipeError, ConnectionResetError):
        raise RoutingAdapterError("SOLVER_FAILED") from None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
