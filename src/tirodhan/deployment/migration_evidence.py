"""Verify the immutable execution template, not just the mutable Job definition."""

from __future__ import annotations

import re
from typing import Any, TypeGuard

API_VERSION = "2025-07-01"


def execution_name(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9-]{1,128}", value) is not None


def migration_template(value: dict[str, Any], image: str) -> bool:
    containers = value.get("properties", {}).get("template", {}).get("containers", [])
    applications = [item for item in containers if item.get("name") == "application"]
    return (
        bool(image)
        and len(applications) == 1
        and applications[0].get("image") == image
        and applications[0].get("command") == ["python", "-m", "tirodhan.deployment.entrypoint"]
        and applications[0].get("args")
        == ["python", "-m", "tirodhan.deployment.job", "--", "alembic", "upgrade", "head"]
    )


def successful_execution(value: dict[str, Any] | None, job_id: str, name: str, image: str) -> bool:
    if value is None or not execution_name(name):
        return False
    expected = job_id + "/executions/" + name
    # ARM's single-execution response may omit id. The exact scoped GET plus name
    # supplies identity in that case; a returned id must never contradict it.
    return (
        str(value.get("id", expected)).casefold() == expected.casefold()
        and value.get("name") == name
        and value.get("properties", {}).get("status") == "Succeeded"
        and migration_template(value, image)
    )
