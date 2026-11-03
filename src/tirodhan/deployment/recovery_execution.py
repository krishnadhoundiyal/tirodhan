"""Read-only trusted-run fencing: an interrupted recovery never blindly starts again."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

from tirodhan.deployment.database_verification_request import Github
from tirodhan.deployment.deployment_admission import AdmissionDenied, Authorization
from tirodhan.deployment.recovery_target import timestamp

REPOSITORY = "krishnadhoundiyal/tirodhan"
WORKFLOW = ".github/workflows/nonprod-reconcile.yml"


def may_start(authorization: Authorization, github: Github | None = None) -> bool:
    client = github or Github()
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if (
        not run_id.isdecimal()
        or not attempt.isdecimal()
        or os.environ.get("GITHUB_JOB") != "reconcile"
    ):
        raise AdmissionDenied("RECOVERY_EXECUTION_CONTEXT_INVALID")
    title = f"NONPROD recover-vault {authorization.run_id}"
    current = client.read(f"actions/runs/{run_id}")

    def trusted(run: dict[str, Any]) -> bool:
        def repository_name(value: Any) -> str:
            return str(value.get("full_name", "")) if isinstance(value, dict) else ""

        return (
            run.get("head_branch") == "main"
            and run.get("path") == WORKFLOW
            and run.get("event") == "workflow_dispatch"
            and repository_name(run.get("repository")) == REPOSITORY
            and repository_name(run.get("head_repository")) == REPOSITORY
            and run.get("display_title") == title
        )

    if (
        not trusted(current)
        or current.get("id") != int(run_id)
        or current.get("head_sha") != authorization.commit
        or current.get("run_attempt") != int(attempt)
        or current.get("status") != "in_progress"
    ):
        raise AdmissionDenied("RECOVERY_EXECUTION_NOT_TRUSTED")
    if int(attempt) != 1:
        return False
    # The deletion generation is authoritative. A new capture/expiry/source revision
    # cannot move this window forward and hide an earlier run of the same session.
    deleted = authorization.claims["bindings"]["recover-vault"]["deleted_vault"]
    date = timestamp(deleted["deletion_date"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urlencode(
        {"event": "workflow_dispatch", "branch": "main", "created": ">=" + date, "per_page": 100}
    )
    history = client.read("actions/workflows/nonprod-reconcile.yml/runs?" + query)
    if (
        type(history.get("total_count")) is not int
        or not 1 <= history["total_count"] <= 100
        or not isinstance(history.get("workflow_runs"), list)
    ):
        raise AdmissionDenied("RECOVERY_RUN_HISTORY_UNVERIFIED")
    rows = history["workflow_runs"]
    if (
        len(rows) != history["total_count"]
        or any(not isinstance(row, dict) or type(row.get("id")) is not int for row in rows)
        or len({row["id"] for row in rows}) != len(rows)
    ):
        raise AdmissionDenied("RECOVERY_RUN_HISTORY_INCOMPLETE_OR_AMBIGUOUS")
    if not any(row.get("id") == int(run_id) and trusted(row) for row in rows):
        raise AdmissionDenied("RECOVERY_CURRENT_RUN_NOT_DISCOVERABLE")
    # All lifecycle jobs use the existing nonprod-release concurrency group. A prior
    # attempt/run of this session can only be observed, never automatically re-started.
    return not any(trusted(row) and row.get("id") != int(run_id) for row in rows)
