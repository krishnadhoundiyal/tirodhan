from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Literal


def positive_seconds(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def write_marker(path: Path, value: dict[str, int | float]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            # Windows readers can briefly deny atomic replacement. Linux ACA
            # needs no retry; do not let local marker reads kill the heartbeat.
            if attempt == 9:
                raise
            time.sleep(0.01)


def completion_reason(
    directory: Path,
    *,
    started_at: float,
    stale_seconds: float,
    startup_seconds: float,
    now: float | None = None,
) -> Literal["done", "stale", "waiting"]:
    current = time.time() if now is None else now
    if (directory / "done.json").exists():
        return "done"
    try:
        marker = json.loads((directory / "heartbeat.json").read_text(encoding="utf-8"))
        timestamp = float(marker["timestamp"])
        if not math.isfinite(timestamp) or timestamp > current + stale_seconds:
            return "stale"
        return "stale" if current - timestamp > stale_seconds else "waiting"
    except (OSError, ValueError, TypeError, KeyError):
        return "stale" if current - started_at > startup_seconds else "waiting"
