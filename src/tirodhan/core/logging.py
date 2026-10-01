from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """Small structured formatter with an intentionally narrow field set."""

    _allowed_extra_fields: frozenset[str] = frozenset(
        {"correlation_id", "environment", "error_type"}
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in self._allowed_extra_fields:
            if name in record.__dict__:
                payload[name] = record.__dict__[name]
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str, log_file_path: Path | None = None) -> None:
    handler: logging.Handler
    if log_file_path is None:
        handler = logging.StreamHandler()
    else:
        handler = logging.FileHandler(log_file_path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(level)

    # HTTP wire/request logging may include geocoding key/address/pin query parameters.
    # Keep third-party HTTP diagnostics off even when application logging is DEBUG.
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)

    for logger_name in ("uvicorn", "uvicorn.error"):
        framework_logger = logging.getLogger(logger_name)
        framework_logger.handlers = [handler]
        framework_logger.setLevel(level)
        framework_logger.propagate = False

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.disabled = True
    access_logger.propagate = False
