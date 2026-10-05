"""Finite-job wrapper: independent heartbeat, protected console capture, exact exit status."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from tirodhan.core.logging import configure_logging
from tirodhan.deployment.coordination import positive_seconds, write_marker

logger = logging.getLogger(__name__)


def capture_console(stream: TextIO, stream_name: str) -> None:
    # Never copy arbitrary command/provider/traceback text into hosted logs.
    # Application JSON already goes directly to the shared file. Console-oriented
    # tools (including Alembic) receive a structured, deliberately redacted record.
    for _line in iter(lambda: stream.readline(8192), ""):
        logger.info("job_console_output", extra={"correlation_id": f"job-{stream_name}"})


def run_command(command: list[str], directory: Path, *, interval: float) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    # EmptyDir survives a container restart within the replica. Do not let stale
    # completion from the preceding process prematurely stop its new sidecar.
    (directory / "done.json").unlink(missing_ok=True)
    write_marker(directory / "heartbeat.json", {"timestamp": time.time()})
    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(interval):
            write_marker(directory / "heartbeat.json", {"timestamp": time.time()})

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    exit_code = 1
    manages_signals = threading.current_thread() is threading.main_thread()
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name == "posix",
        ) as process:
            assert process.stdout is not None and process.stderr is not None
            readers = [
                threading.Thread(
                    target=capture_console, args=(process.stdout, "stdout"), daemon=True
                ),
                threading.Thread(
                    target=capture_console, args=(process.stderr, "stderr"), daemon=True
                ),
            ]
            for reader in readers:
                reader.start()

            def forward(signum: int, frame: object) -> None:
                if process.poll() is None:
                    if sys.platform != "win32":
                        os.killpg(process.pid, signum)
                    else:
                        process.terminate()

            if manages_signals:
                signal.signal(signal.SIGTERM, forward)
                signal.signal(signal.SIGINT, forward)
            return_code = process.wait()
            exit_code = return_code if return_code >= 0 else 128 - return_code
            for reader in readers:
                reader.join(timeout=1)
    finally:
        if manages_signals:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
        stopped.set()
        heartbeat_thread.join(timeout=interval + 1)
        logger.info("job_completed")
        write_marker(directory / "done.json", {"exit_code": exit_code, "timestamp": time.time()})
    return exit_code


def main() -> None:
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("A finite-job command is required")
    directory = Path(os.environ.get("LOG_SHARED_DIR", "/var/log/tirodhan"))
    directory.mkdir(parents=True, exist_ok=True)
    configure_logging(
        os.environ.get("TIRODHAN_LOG_LEVEL", "INFO"),
        Path(os.environ.get("TIRODHAN_LOG_FILE_PATH", str(directory / "application.jsonl"))),
    )
    raise SystemExit(
        run_command(
            command,
            directory,
            interval=positive_seconds("JOB_HEARTBEAT_SECONDS", 5),
        )
    )


if __name__ == "__main__":
    main()
