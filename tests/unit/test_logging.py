import json
import logging
from pathlib import Path

from tirodhan.core.logging import JsonFormatter, configure_logging


def test_json_formatter_only_emits_allowlisted_context() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="application_started",
        args=(),
        exc_info=None,
    )
    record.environment = "test"
    record.database_url = "postgresql+asyncpg://user:secret@database/test"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["environment"] == "test"
    assert "database_url" not in payload
    assert "secret" not in JsonFormatter().format(record)


def test_configured_log_file_uses_safe_json_formatter(tmp_path: Path) -> None:
    log_file = tmp_path / "application.jsonl"
    configure_logging("INFO", log_file)

    logging.getLogger("test.file").info(
        "application_started",
        extra={
            "environment": "test",
            "database_url": "postgresql+asyncpg://user:secret@database/test",
        },
    )

    payload = json.loads(log_file.read_text(encoding="utf-8"))
    assert payload["message"] == "application_started"
    assert payload["environment"] == "test"
    assert "database_url" not in payload
    assert "secret" not in log_file.read_text(encoding="utf-8")
