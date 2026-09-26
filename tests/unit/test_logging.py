import json
import logging

from tirodhan.core.logging import JsonFormatter


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
