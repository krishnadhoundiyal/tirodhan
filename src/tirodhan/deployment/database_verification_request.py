"""Bounded, idempotent exchange with the independently protected SQL verifier job."""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

from tirodhan.deployment.deployment_admission import (
    AdmissionDenied,
    Authorization,
    digest,
    require_release_authorization,
)

REPOSITORY = "krishnadhoundiyal/tirodhan"
DEPLOY_WORKFLOW = ".github/workflows/nonprod-deploy.yml"
VERIFY_WORKFLOW = "nonprod-database-verify.yml"
CONSUMERS = {
    "foundation-ready": {"migration-plan"},
    "migration-plan": {"migration-plan"},
    "migration-apply": {"migration-apply", "migration-start"},
    "runtime-plan": {"runtime-plan"},
    "runtime-apply": {"runtime-apply"},
}
WAIT_SECONDS = 900


class Github:
    def __init__(self, deadline: float | None = None) -> None:
        self.deadline = deadline or time.monotonic() + WAIT_SECONDS

    def command(self, *args: str, payload: dict[str, Any] | None = None) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AdmissionDenied("DATABASE_EVIDENCE_DEADLINE_EXCEEDED")
        try:
            result = subprocess.run(
                ["gh", "api", "--hostname", "github.com", *args],
                input=json.dumps(payload).encode() if payload is not None else None,
                capture_output=True,
                timeout=min(30, remaining),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdmissionDenied("DATABASE_EVIDENCE_GITHUB_UNAVAILABLE") from exc
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise AdmissionDenied("DATABASE_EVIDENCE_GITHUB_UNAVAILABLE")
        return result.stdout

    def read(self, path: str) -> dict[str, Any]:
        try:
            value = json.loads(self.command(f"repos/{REPOSITORY}/{path}"))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, TypeError) as exc:
            raise AdmissionDenied("DATABASE_EVIDENCE_GITHUB_MALFORMED") from exc

    def dispatch(self, request: dict[str, str]) -> None:
        self.command(
            "--method",
            "POST",
            f"repos/{REPOSITORY}/actions/workflows/{VERIFY_WORKFLOW}/dispatches",
            "--input",
            "-",
            payload={
                "ref": "main",
                "inputs": {
                    "request_id": request["request_id"],
                    "authorization_id": request["release_session"],
                    "request": json.dumps(request, sort_keys=True),
                },
            },
        )


def consumer_context(authorization: Authorization) -> dict[str, str]:
    job = os.environ.get("GITHUB_JOB", "")
    if (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != f"{REPOSITORY}/{DEPLOY_WORKFLOW}@refs/heads/main"
        or os.environ.get("GITHUB_SHA") != authorization.commit
        or authorization.operation not in CONSUMERS.get(job, set())
    ):
        raise AdmissionDenied("DATABASE_EVIDENCE_CONSUMER_CONTEXT_INVALID")
    run, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not run.isdecimal() or not attempt.isdecimal():
        raise AdmissionDenied("DATABASE_EVIDENCE_CONSUMER_CONTEXT_INVALID")
    value = {
        "repository": REPOSITORY,
        "workflow": DEPLOY_WORKFLOW,
        "run_id": run,
        "attempt": attempt,
        "job": job,
        "operation": authorization.operation,
        "release_session": authorization.run_id,
        "commit": authorization.commit,
        "configuration_hash": authorization.configuration_hash,
        "contract_hash": authorization.contract_hash,
        "principal_id": authorization.principal_id,
        "authorization_id": str(authorization.claims["jti"]),
    }
    return value | {"request_id": digest(value)}


def validate_request(
    authorization: Authorization,
    request: Any,
    github: Github,
) -> dict[str, str]:
    fields = {
        "repository",
        "workflow",
        "run_id",
        "attempt",
        "job",
        "operation",
        "release_session",
        "commit",
        "configuration_hash",
        "contract_hash",
        "principal_id",
        "authorization_id",
        "request_id",
    }
    if (
        not isinstance(request, dict)
        or set(request) != fields
        or any(not isinstance(v, str) or not v for v in request.values())
    ):
        raise AdmissionDenied("DATABASE_VERIFICATION_REQUEST_MALFORMED")
    binding = authorization.claims.get("bindings", {}).get("database-verify", {})
    if (
        authorization.operation != "database-verify"
        or authorization.identity_kind != "database_verification"
        or request["request_id"] != digest({k: v for k, v in request.items() if k != "request_id"})
        or request["repository"] != REPOSITORY
        or request["workflow"] != DEPLOY_WORKFLOW
        or request["commit"] != authorization.commit
        or request["configuration_hash"] != authorization.configuration_hash
        or request["contract_hash"] != authorization.contract_hash
        or request["release_session"] != authorization.run_id
        or request["operation"] not in CONSUMERS.get(request["job"], set())
        or request["operation"] not in binding.get("consumer_operations", [])
        or binding.get("database_verification_authorized") is not True
        or not request["run_id"].isdecimal()
        or not request["attempt"].isdecimal()
    ):
        raise AdmissionDenied("DATABASE_VERIFICATION_REQUEST_UNAUTHORIZED")
    run = github.read(f"actions/runs/{request['run_id']}")
    if (
        run.get("head_sha") != authorization.commit
        or run.get("head_branch") != "main"
        or run.get("path") != DEPLOY_WORKFLOW
        or run.get("status") != "in_progress"
        or run.get("event") not in {"push", "workflow_dispatch"}
        or str(run.get("run_attempt")) != request["attempt"]
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
    ):
        raise AdmissionDenied("DATABASE_VERIFICATION_CONSUMER_NOT_TRUSTED_OR_ACTIVE")
    jobs = github.read(
        f"actions/runs/{request['run_id']}/attempts/{request['attempt']}/jobs?per_page=100"
    )
    active = [row for row in jobs.get("jobs", []) if row.get("name") == request["job"]]
    if (
        jobs.get("total_count", 101) > 100
        or len(active) != 1
        or active[0].get("status") != "in_progress"
    ):
        raise AdmissionDenied("DATABASE_VERIFICATION_CONSUMER_NOT_TRUSTED_OR_ACTIVE")
    verify_request_artifact(github, request)
    return dict(request)


def matching_run(github: Github, request: dict[str, str]) -> dict[str, Any] | None:
    # Page the bounded recent window: concurrent requests must not hide this request.
    found: list[dict[str, Any]] = []
    for page in range(1, 11):
        result = github.read(
            f"actions/workflows/{VERIFY_WORKFLOW}/runs?event=workflow_dispatch&head_sha={request['commit']}&per_page=100&page={page}"
        )
        rows = result.get("workflow_runs")
        if not isinstance(rows, list):
            raise AdmissionDenied("DATABASE_VERIFICATION_RUNS_UNAVAILABLE")
        found.extend(
            row
            for row in rows
            if row.get("display_title") == "SQL verification " + request["request_id"]
        )
        if len(rows) < 100:
            break
    else:
        raise AdmissionDenied("DATABASE_VERIFICATION_RUN_HISTORY_UNVERIFIED")
    if len(found) > 1:
        raise AdmissionDenied("DATABASE_VERIFICATION_RUN_AMBIGUOUS")
    if not found:
        return None
    row = found[0]
    if (
        row.get("head_sha") != request["commit"]
        or row.get("head_branch") != "main"
        or row.get("path") != ".github/workflows/" + VERIFY_WORKFLOW
        or row.get("event") != "workflow_dispatch"
        or type(row.get("id")) is not int
    ):
        raise AdmissionDenied("DATABASE_VERIFICATION_RUN_UNTRUSTED")
    return dict(row)


def artifact_bytes(github: Github, run_id: int, name: str) -> bytes:
    values = github.read(f"actions/runs/{run_id}/artifacts?per_page=100")
    rows = [row for row in values.get("artifacts", []) if row.get("name") == name]
    if (
        len(rows) != 1
        or rows[0].get("expired") is not False
        or not 0 < rows[0].get("size_in_bytes", 0) <= 65536
    ):
        raise AdmissionDenied("DATABASE_VERIFICATION_ARTIFACT_UNAVAILABLE")
    artifact_id = rows[0].get("id")
    if type(artifact_id) is not int:
        raise AdmissionDenied("DATABASE_VERIFICATION_ARTIFACT_UNAVAILABLE")
    value = github.command(f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip")
    if len(value) > 65536:
        raise AdmissionDenied("DATABASE_VERIFICATION_ARTIFACT_TOO_LARGE")
    return value


def request_artifact_name(request: dict[str, str]) -> str:
    return f"database-requests-{request['job']}-{request['attempt']}"


def verify_request_artifact(github: Github, request: dict[str, str]) -> None:
    import re

    value = artifact_bytes(github, int(request["run_id"]), request_artifact_name(request))
    try:
        with zipfile.ZipFile(io.BytesIO(value)) as archive:
            names = archive.namelist()
            if (
                not names
                or len(names) > len(CONSUMERS[request["job"]])
                or len(names) != len(set(names))
                or any(not re.fullmatch(r"[a-f0-9]{64}\.json", name) for name in names)
                or any(archive.getinfo(name).file_size > 8192 for name in names)
            ):
                raise ValueError
            with archive.open(request["request_id"] + ".json") as handle:
                body = handle.read(8193)
            if len(body) > 8192:
                raise ValueError
            actual = json.loads(body)
            if actual != request:
                raise ValueError
    except (ValueError, KeyError, zipfile.BadZipFile, UnicodeError) as exc:
        raise AdmissionDenied("DATABASE_REQUEST_ARTIFACT_UNVERIFIED") from exc


def download_evidence(github: Github, run_id: int, request: dict[str, str]) -> str:
    value = artifact_bytes(github, run_id, "database-evidence-" + request["request_id"])
    try:
        with zipfile.ZipFile(io.BytesIO(value)) as archive:
            if archive.namelist() != ["database-evidence.jwt"]:
                raise ValueError
            info = archive.getinfo("database-evidence.jwt")
            if not 0 < info.file_size <= 32768:
                raise ValueError
            with archive.open(info) as handle:
                body = handle.read(32769)
            if len(body) > 32768:
                raise ValueError
            return body.decode().strip()
    except (ValueError, KeyError, zipfile.BadZipFile, UnicodeError) as exc:
        raise AdmissionDenied("DATABASE_VERIFICATION_ARTIFACT_MALFORMED") from exc


def refresh_database_evidence(authorization: Authorization) -> None:
    try:
        _refresh_database_evidence(authorization)
    except AdmissionDenied:
        raise
    except Exception as exc:
        raise AdmissionDenied("DATABASE_VERIFICATION_UNVERIFIED") from exc


def _refresh_database_evidence(authorization: Authorization) -> None:
    from tirodhan.deployment.prerequisites import REQUIRED

    if (
        "database" not in REQUIRED[authorization.operation]
        or os.environ.get("GITHUB_ACTIONS") != "true"
    ):
        return
    os.environ.pop("TIRODHAN_DATABASE_PREREQUISITE_EVIDENCE", None)
    request = consumer_context(authorization)
    github = Github()
    verify_request_artifact(github, request)
    directory = Path(os.environ["RUNNER_TEMP"]) / "tirodhan-database-requests"
    directory.mkdir(mode=0o700, exist_ok=True)
    claim = directory / (request["request_id"] + ".json")
    try:
        with claim.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(request, sort_keys=True))
        owner = True
    except FileExistsError:
        # The filename is the complete request's digest. A concurrent writer may
        # still be writing the audit body; existence alone prevents another dispatch.
        owner = False
    current = matching_run(github, request)
    if owner and current is None:
        try:
            github.dispatch(request)
        except AdmissionDenied:
            pass  # Dispatch may have committed. Reconcile by request ID; never blindly resend.
    while time.monotonic() < github.deadline:
        current = current or matching_run(github, request)
        if current is not None and current.get("status") == "completed":
            if current.get("conclusion") != "success":
                raise AdmissionDenied("DATABASE_VERIFICATION_DID_NOT_SUCCEED")
            token = download_evidence(github, current["id"], request)
            os.environ["TIRODHAN_DATABASE_PREREQUISITE_EVIDENCE"] = token
            return  # The controller now validates signature, freshness and exact consumer binding.
        time.sleep(min(5, max(0, github.deadline - time.monotonic())))
        current = None
    raise AdmissionDenied("DATABASE_EVIDENCE_DEADLINE_EXCEEDED")


def prepare_requests() -> None:
    operations = os.environ["DATABASE_REQUEST_OPERATIONS"].split(",")
    job = os.environ.get("GITHUB_JOB", "")
    if set(operations) != CONSUMERS.get(job) or len(operations) != len(set(operations)):
        raise AdmissionDenied("DATABASE_REQUEST_OPERATIONS_INVALID")
    values = [consumer_context(require_release_authorization(op)) for op in operations]
    directory = Path(os.environ["RUNNER_TEMP"]) / (
        "database-request-inputs-" + job + "-" + values[0]["attempt"]
    )
    directory.mkdir(mode=0o700, exist_ok=True)
    for value in values:
        path = directory / (value["request_id"] + ".json")
        body = json.dumps(value, sort_keys=True)
        if path.exists():
            if path.read_text() != body:
                raise AdmissionDenied("DATABASE_REQUEST_REPLAY_CONFLICT")
        else:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(body)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as handle:
        handle.write(f"directory={directory}\nartifact_name={request_artifact_name(values[0])}\n")


if __name__ == "__main__":
    try:
        prepare_requests()
    except Exception:
        raise SystemExit("DATABASE_REQUEST_NOT_AUTHORIZED: no request artifact prepared") from None
