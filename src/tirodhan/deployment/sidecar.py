"""Bounded Fluent Bit supervisor; also runs standalone in the deployment sidecar image."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .coordination import completion_reason, positive_seconds


def stop_and_flush(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    if process.poll() is not None:
        return
    process.terminate()  # Fluent Bit SIGTERM initiates its graceful final flush.
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def supervise(
    command: list[str],
    directory: Path,
    *,
    is_job: bool,
    stale_seconds: float = 30,
    startup_seconds: float = 60,
    grace_seconds: float = 15,
    poll_seconds: float = 1,
) -> int:
    started_at = time.time()
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        stopping = False

        def shutdown(signum: int, frame: object) -> None:
            nonlocal stopping
            stopping = True

        old_term = signal.signal(signal.SIGTERM, shutdown)
        old_int = signal.signal(signal.SIGINT, shutdown)
        try:
            while process.poll() is None:
                if stopping or (
                    is_job
                    and completion_reason(
                        directory,
                        started_at=started_at,
                        stale_seconds=stale_seconds,
                        startup_seconds=startup_seconds,
                    )
                    != "waiting"
                ):
                    stop_and_flush(process, grace_seconds)
                    return 0
                time.sleep(poll_seconds)
            return process.returncode or 0
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            stop_and_flush(process, grace_seconds)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode not in {"service", "job"}:
        raise SystemExit("Specify service or job")
    raise SystemExit(
        supervise(
            ["/fluent-bit/bin/fluent-bit", "-c", "/fluent-bit/etc/tirodhan.conf"],
            Path(os.environ.get("LOG_SHARED_DIR", "/var/log/tirodhan")),
            is_job=mode == "job",
            stale_seconds=positive_seconds("JOB_STALE_SECONDS", 30),
            startup_seconds=positive_seconds("JOB_STARTUP_SECONDS", 60),
            grace_seconds=positive_seconds("LOG_FLUSH_GRACE_SECONDS", 15),
        )
    )


if __name__ == "__main__":
    main()
