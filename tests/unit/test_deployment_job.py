from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tirodhan.deployment.coordination import completion_reason, positive_seconds, write_marker
from tirodhan.deployment.job import capture_console, run_command
from tirodhan.deployment.sidecar import stop_and_flush, supervise


@pytest.mark.parametrize("code", [0, 17, 66, 255])
def test_job_preserves_exit_code_and_completion(tmp_path: Path, code: int) -> None:
    result = run_command(
        [sys.executable, "-c", f"raise SystemExit({code})"], tmp_path, interval=0.02
    )
    assert result == code
    done = json.loads((tmp_path / "done.json").read_text())
    assert done["exit_code"] == code
    assert done["timestamp"] >= json.loads((tmp_path / "heartbeat.json").read_text())["timestamp"]


def test_heartbeat_advances_while_command_runs(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import time; time.sleep(0.7)"]
    thread = threading.Thread(
        target=run_command, args=(command, tmp_path), kwargs={"interval": 0.02}
    )
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not (tmp_path / "heartbeat.json").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        first = json.loads((tmp_path / "heartbeat.json").read_text())["timestamp"]
        time.sleep(0.15)
        second = json.loads((tmp_path / "heartbeat.json").read_text())["timestamp"]
        assert second > first
        assert not (tmp_path / "done.json").exists()
    finally:
        thread.join(timeout=4)
    assert not thread.is_alive()
    assert json.loads((tmp_path / "done.json").read_text())["exit_code"] == 0


def test_console_is_structured_but_never_copies_command_output(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Alembic fileConfig can disable already-instantiated deployment loggers.
    # Production wrapper is initialized in its own process; isolate this test too.
    from tirodhan.deployment.job import logger

    monkeypatch.setattr(logger, "disabled", False)
    with caplog.at_level(logging.INFO):
        capture_console(
            io.StringIO('sensitive-placeholder\n{"message":"private_placeholder"}\n'), "stderr"
        )
    assert len(caplog.records) == 2
    assert all(record.message == "job_console_output" for record in caplog.records)
    assert "sensitive-placeholder" not in caplog.text
    assert "private_placeholder" not in caplog.text


def test_actual_wrapper_writes_json_and_redacts_stdout_stderr(tmp_path: Path) -> None:
    logfile = tmp_path / "application.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tirodhan.deployment.job",
            "--",
            sys.executable,
            "-c",
            "import sys; print('private-placeholder');"
            " print('credential-placeholder', file=sys.stderr);"
            " raise SystemExit(19)",
        ],
        env=os.environ | {"LOG_SHARED_DIR": str(tmp_path), "TIRODHAN_LOG_FILE_PATH": str(logfile)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 19
    records = [json.loads(line) for line in logfile.read_text().splitlines()]
    assert sum(item["message"] == "job_console_output" for item in records) == 2
    assert records[-1]["message"] == "job_completed"
    assert "private-placeholder" not in logfile.read_text()
    assert "credential-placeholder" not in logfile.read_text()
    assert result.stdout == "" and result.stderr == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal exit status used by ACA")
def test_command_killed_by_signal_preserves_posix_exit_status(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"]
    assert run_command(command, tmp_path, interval=0.02) == 143
    assert json.loads((tmp_path / "done.json").read_text())["exit_code"] == 143


@pytest.mark.parametrize("kind", ["normal", "stale", "missing"])
def test_sidecar_terminates_on_completion_or_missing_heartbeat(tmp_path: Path, kind: str) -> None:
    if kind == "normal":
        write_marker(tmp_path / "done.json", {"exit_code": 0, "timestamp": time.time()})
    if kind == "stale":
        write_marker(tmp_path / "heartbeat.json", {"timestamp": time.time() - 60})
    started = time.monotonic()
    result = supervise(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        is_job=True,
        stale_seconds=0.05,
        startup_seconds=0.05,
        poll_seconds=0.01,
        grace_seconds=0.2,
    )
    assert result == 0
    assert time.monotonic() - started < 3


def test_killed_main_is_detected_from_real_heartbeat(tmp_path: Path) -> None:
    main = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tirodhan.deployment.job",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
        ],
        env=os.environ
        | {
            "LOG_SHARED_DIR": str(tmp_path),
            "JOB_HEARTBEAT_SECONDS": "0.02",
            "TIRODHAN_LOG_FILE_PATH": str(tmp_path / "application.jsonl"),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "heartbeat.json").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "heartbeat.json").exists()
        main.kill()
        main.wait(timeout=5)
        assert not (tmp_path / "done.json").exists()
        assert (
            supervise(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                tmp_path,
                is_job=True,
                stale_seconds=0.05,
                startup_seconds=0.1,
                grace_seconds=0.2,
                poll_seconds=0.01,
            )
            == 0
        )
    finally:
        if main.poll() is None:
            main.kill()
            main.wait(timeout=5)


def test_stale_detection_has_bounded_startup_and_rejects_invalid_markers(tmp_path: Path) -> None:
    options = {"started_at": 100.0, "stale_seconds": 30.0, "startup_seconds": 60.0}
    assert completion_reason(tmp_path, now=150, **options) == "waiting"
    assert completion_reason(tmp_path, now=161, **options) == "stale"
    write_marker(tmp_path / "heartbeat.json", {"timestamp": 150})
    assert completion_reason(tmp_path, now=151, **options) == "waiting"
    assert completion_reason(tmp_path, now=181, **options) == "stale"
    write_marker(tmp_path / "heartbeat.json", {"timestamp": float("nan")})
    assert completion_reason(tmp_path, now=151, **options) == "stale"


def test_supervisor_kills_unresponsive_child_after_bounded_grace() -> None:
    from unittest.mock import Mock

    child = Mock()
    child.poll.return_value = None
    child.wait.side_effect = [subprocess.TimeoutExpired("fake", 0.01), 0]
    stop_and_flush(child, 0.01)
    child.terminate.assert_called_once()
    child.kill.assert_called_once()
    assert child.wait.call_args_list[0].kwargs == {"timeout": 0.01}


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_coordination_timeouts_are_positive_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("JOB_STALE_SECONDS", value)
    with pytest.raises(ValueError):
        positive_seconds("JOB_STALE_SECONDS", 30)
