"""Fail-stop ownership for the finite POSIX notification worker and its children."""

import os
import sys
from typing import NoReturn, Protocol

if sys.platform != "win32":
    from os import getpgrp, killpg, setpgid
    from signal import SIGKILL


class LaunchOwner(Protocol):
    def abort(self) -> NoReturn: ...


class ProcessGroupOwner:
    def __init__(self) -> None:
        # Establish this boundary in the dedicated worker, never the transactional API.
        # Children inherit membership at creation, before asyncio can return a PID/handle.
        if sys.platform == "win32":
            raise RuntimeError("WEB_PUSH_REQUIRES_POSIX_WORKER_OWNERSHIP")
        else:
            # The production Dockerfile already uses CPython 3.12. Its Linux vfork
            # path releases the GIL while exec may stall (CPython GH-104372); older
            # runtimes must not promise that a Python watchdog thread can run then.
            if sys.implementation.name != "cpython" or sys.version_info < (3, 12):
                raise RuntimeError("WEB_PUSH_REQUIRES_CPYTHON_312_WORKER")
            if getpgrp() != os.getpid():
                setpgid(0, 0)
            self.group_id = getpgrp()
            if self.group_id != os.getpid():
                raise RuntimeError("WEB_PUSH_PROCESS_GROUP_UNAVAILABLE")

    def abort(self) -> NoReturn:
        # Never detach/cancel unknown launch ownership and continue processing. Kernel
        # group membership covers even children whose asyncio handoff never completes.
        try:
            if sys.platform != "win32":
                killpg(self.group_id, SIGKILL)
        finally:
            # Also exits a namespace's PID 1, which may treat group signals specially;
            # container teardown then terminates any remaining namespace processes.
            os._exit(1)
