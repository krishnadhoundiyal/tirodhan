"""Read-only evidence preparation; no signing keys, setting writes or workflow dispatch."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from tirodhan.deployment.database_verification_request import Github
from tirodhan.deployment.deployment_admission import (
    ROOT,
    AdmissionDenied,
    configuration,
    contract_digest,
    digest,
)
from tirodhan.deployment.lifecycle_evidence import Observations, Runner, plan_prior_state
from tirodhan.deployment.release_custody import operator_only, private_directory, private_write
from tirodhan.deployment.release_setup import (
    DESTINATIONS,
    REPOSITORY,
    expected_bindings,
    validate_evidence,
)


class ReadOnlyGithub(Github):
    def get(self, path: str) -> Any:
        return json.loads(self.command(f"repos/{REPOSITORY}/{path}"))

    def threads(self, number: int) -> list[dict[str, Any]]:
        query = """query($number:Int!){repository(owner:"krishnadhoundiyal",name:"tirodhan"){
          pullRequest(number:$number){reviewThreads(first:100){pageInfo{hasNextPage}
          nodes{isResolved isOutdated}}}}}"""
        value = json.loads(
            self.command("graphql", "-f", f"query={query}", "-F", f"number={number}")
        )
        threads = value["data"]["repository"]["pullRequest"]["reviewThreads"]
        if threads["pageInfo"]["hasNextPage"]:
            raise AdmissionDenied("REVIEW_THREAD_HISTORY_EXCEEDS_SAFE_CAPTURE_BOUND")
        return [row for row in threads["nodes"] if row["isResolved"] is not True]

    def download_artifact(self, artifact_id: int, *, limit: int = 40 * 1024 * 1024) -> bytes:
        """Bound binary streaming separately from the existing 1 MiB JSON budget."""

        def remaining() -> float:
            value = self.deadline - time.monotonic()
            if value <= 0:
                raise AdmissionDenied("PLAN_ARTIFACT_DOWNLOAD_DEADLINE_EXCEEDED")
            return value

        def secure_redirect(response: httpx.Response) -> None:
            if response.is_redirect:
                target = response.url.join(response.headers["location"])
                if target.scheme != "https" or target.username or target.password:
                    raise AdmissionDenied("PLAN_ARTIFACT_REDIRECT_UNTRUSTED")

        try:
            token = Runner().run(
                "gh",
                "auth",
                "token",
                "--hostname",
                "github.com",
                timeout=max(1, min(10, int(remaining()))),
            )
            if not token:
                raise AdmissionDenied("PLAN_ARTIFACT_AUTHENTICATION_UNAVAILABLE")
            with httpx.Client(
                timeout=min(30, remaining()),
                follow_redirects=True,
                max_redirects=5,
                trust_env=False,
                event_hooks={"response": [secure_redirect]},
            ) as client:
                # httpx strips Authorization when a redirect changes origin. Signed
                # artifact URLs and credentials never appear in diagnostics.
                with client.stream(
                    "GET",
                    f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip",
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json",
                    },
                ) as response:
                    response.raise_for_status()
                    if int(response.headers.get("content-length", "0")) > limit:
                        raise AdmissionDenied("PLAN_ARTIFACT_DOWNLOAD_TOO_LARGE")
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        remaining()
                        if len(content) + len(chunk) > limit:
                            raise AdmissionDenied("PLAN_ARTIFACT_DOWNLOAD_TOO_LARGE")
                        content.extend(chunk)
                    return bytes(content)
        except AdmissionDenied:
            raise
        except Exception:
            raise AdmissionDenied("PLAN_ARTIFACT_DOWNLOAD_UNAVAILABLE") from None


def collect_review(
    github: ReadOnlyGithub, number: int, commit: str, code_hash: str
) -> dict[str, Any]:
    pull = github.get(f"pulls/{number}")
    source = pull["head"]["sha"]
    target_tree = github.get(f"git/commits/{commit}")["tree"]["sha"]
    source_tree = github.get(f"git/commits/{source}")["tree"]["sha"]
    response = github.get(f"actions/runs?head_sha={source}&event=pull_request&per_page=100")
    if response["total_count"] > 100:
        raise AdmissionDenied("CI_HISTORY_EXCEEDS_SAFE_CAPTURE_BOUND")
    runs = []
    for workflow in ("pr-checks.yml", "nonprod-lifecycle-ci.yml"):
        candidates = [
            r for r in response["workflow_runs"] if r["path"] == ".github/workflows/" + workflow
        ]
        if not candidates:
            raise AdmissionDenied("REQUIRED_CI_EVIDENCE_MISSING")
        runs.append(max(candidates, key=lambda row: row["id"]))
    reviews = github.get(f"pulls/{number}/reviews?per_page=100")
    if len(reviews) >= 100:
        raise AdmissionDenied("REVIEW_HISTORY_EXCEEDS_SAFE_CAPTURE_BOUND")
    candidates = [
        r
        for r in reviews
        if r.get("commit_id") == source and r.get("user", {}).get("login") == "coderabbitai[bot]"
    ]
    if not candidates:
        raise AdmissionDenied("CODERABBIT_REVIEW_MISSING")
    evidence = {
        "repository": REPOSITORY,
        "target_commit": commit,
        "contract_hash": code_hash,
        "target_tree": target_tree,
        "source_tree": source_tree,
        "pull": pull,
        "workflow_runs": runs,
        "review": max(candidates, key=lambda r: r["id"]),
        "unresolved_threads": github.threads(number),
    }
    validate_evidence(evidence, commit)
    return evidence


def capture_plan_artifact(
    github: ReadOnlyGithub,
    artifact_id: int,
    plan: Path,
    operation: str,
    *,
    commit: str,
    session: str,
) -> dict[str, Any]:
    """Require the native plan from the exact trusted-main workflow, not an arbitrary local plan."""
    mode = operation.removesuffix("-apply")
    artifact = github.get(f"actions/artifacts/{artifact_id}")
    run_id = artifact["workflow_run"]["id"]
    run = github.get(f"actions/runs/{run_id}")
    workflow = f".github/workflows/nonprod-{'foundation' if mode == 'foundation' else 'deploy'}.yml"
    if (
        artifact.get("expired") is not False
        or artifact.get("name") != mode + "-plan"
        or artifact["workflow_run"].get("head_sha") != commit
        or run.get("head_sha") != commit
        or run.get("head_branch") != "main"
        or run.get("path") != workflow
        or run.get("event") != "workflow_dispatch"
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
        or run.get("status") not in {"in_progress", "waiting"}
    ):
        raise AdmissionDenied("PLAN_ARTIFACT_NOT_FROM_ACTIVE_TRUSTED_RELEASE")
    # The operator supplies the release session; the existing consumer validates it
    # against the protected workflow's authorization_id before accepting this token.
    jobs = github.get(f"actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100")
    if jobs.get("total_count", 101) > 100 or not any(
        r.get("name") == mode + "-plan" and r.get("conclusion") == "success"
        for r in jobs.get("jobs", [])
    ):
        raise AdmissionDenied("PLAN_JOB_NOT_SUCCESSFUL")
    data = github.download_artifact(artifact_id)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.namelist() != [mode + ".tfplan"]:
            raise AdmissionDenied("PLAN_ARTIFACT_CONTENTS_UNEXPECTED")
        info = archive.infolist()[0]
        if info.file_size > 32 * 1024 * 1024:
            raise AdmissionDenied("PLAN_ARTIFACT_TOO_LARGE")
        if archive.read(info) != plan.read_bytes():
            raise AdmissionDenied("LOCAL_PLAN_DOES_NOT_MATCH_TRUSTED_ARTIFACT")
    return {
        "artifact_id": artifact_id,
        "run_id": run_id,
        "attempt": run["run_attempt"],
        "workflow": workflow,
        "release_session": session,
    }


def prepare(
    kind: str,
    *,
    session: str,
    expires_at: int,
    review_pr: int,
    github: ReadOnlyGithub,
    runner: Runner,
    config: dict[str, Any],
    apply_operation: str | None = None,
    plan: Path | None = None,
    artifact_id: int | None = None,
    database_key_id: str | None = None,
) -> dict[str, Any]:
    operator_only()
    # Authorize only the clean checked-out main commit with exact reviewed tree provenance.
    if runner.run("git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all"):
        raise AdmissionDenied("AUTHORIZE_ONLY_CLEAN_COMMITTED_SOURCE")
    commit = runner.run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    if github.get("commits/main")["sha"] != commit:
        raise AdmissionDenied("CHECKOUT_IS_NOT_CURRENT_MAIN")
    code_hash = contract_digest()
    evidence = collect_review(github, review_pr, commit, code_hash)
    if runner.run("git", "-C", str(ROOT), "rev-parse", "HEAD^{tree}") != evidence["target_tree"]:
        raise AdmissionDenied("LOCAL_REVIEWED_TREE_MISMATCH")
    bindings = expected_bindings(kind, apply_operation)
    secret, destination, identity, environment = DESTINATIONS[kind]
    client = config[
        "database_verification_client_id" if kind == "database" else "deployment_client_id"
    ]
    account = runner.json("az", "account", "show", "--output", "json")
    if account.get("tenantId") != config["terraform"]["tenant_id"]:
        raise AdmissionDenied("IDENTITY_LOOKUP_TENANT_MISMATCH")
    principal = runner.json(
        "az",
        "ad",
        "sp",
        "show",
        "--id",
        client,
        "--query",
        "{id:id,appId:appId}",
        "--output",
        "json",
    )
    principal["tenant_id"] = account["tenantId"]
    request = {
        "schema_version": 1,
        "kind": kind,
        "secret_name": secret,
        "destination": destination,
        "environment": environment,
        "identity_kind": identity,
        "repository": REPOSITORY,
        "principal_id": principal["id"],
        "principal": principal,
        "commit": commit,
        "configuration_hash": digest(config),
        "contract_hash": code_hash,
        "release_session": session,
        "expires_at": expires_at,
        "captured_at": int(time.time()),
        "evidence": evidence,
        "apply_operation": apply_operation,
        "database_key_id": database_key_id,
        "operations": sorted(bindings),
    }
    if kind == "apply":
        if plan is None or artifact_id is None:
            raise AdmissionDenied("REVIEWED_GITHUB_PLAN_ARTIFACT_REQUIRED")
        request["plan_artifact"] = capture_plan_artifact(
            github, artifact_id, plan, str(apply_operation), commit=commit, session=session
        )
        version = runner.json("terraform", "version", "-json")
        if version.get("terraform_version") != "1.13.5":
            raise AdmissionDenied("PINNED_TERRAFORM_REQUIRED_FOR_REVIEW")
        observation = Observations(config, runner)
        claims = observation.identity()
        # The authorizer's existing approved observer may differ from D. This
        # read grants no deployment authority; live prerequisites recheck D.
        if not claims.get("oid"):
            raise AdmissionDenied("STATE_CAPTURE_OBSERVER_UNVERIFIED")
        request["state_observer_principal_id"] = claims["oid"]
        state = observation.backend()
        if state.kind != "INITIALIZED" or state.document is None or not state.etag:
            raise AdmissionDenied("AUTHORITATIVE_INITIALIZED_STATE_REQUIRED")
        prior = plan_prior_state(plan)
        if any(prior.get(k) != state.document.get(k) for k in ("lineage", "serial")):
            raise AdmissionDenied("PLAN_STATE_IDENTITY_CHANGED")
        shown = runner.json("terraform", "show", "-json", str(plan.resolve()))
        from tirodhan.deployment.check_state import populated_key_paths

        if populated_key_paths(shown) or any(
            shown.get("variables", {}).get(name, {}).get("value") != value
            for name, value in config["terraform"].items()
        ):
            raise AdmissionDenied("PLAN_CONFIGURATION_OR_SECRET_HYGIENE_INVALID")
        request["state"] = {"kind": state.kind, "etag": state.etag, "document": state.document}
        request["plan_review"] = {
            "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
            "state_fingerprint": state.fingerprint,
            "lineage": state.document["lineage"],
            "serial": state.document["serial"],
            "etag": state.etag,
            "reviewed_by_operator": False,
        }
    return request


def enrollment_inventory(github: ReadOnlyGithub) -> dict[str, Any]:
    """Presence is not proof of a secret's unreadable value. Never overwrite it automatically."""
    result: dict[str, Any] = {}
    groups = {
        "repository": ("actions/secrets", {"NONPROD_RELEASE_AUTHORIZATION"}),
        "nonprod": ("environments/nonprod/secrets", {"NONPROD_APPLY_AUTHORIZATION"}),
        "nonprod-database": (
            "environments/nonprod-database/secrets",
            {"NONPROD_DATABASE_VERIFY_AUTHORIZATION", "NONPROD_DATABASE_EVIDENCE_PRIVATE_KEY_PEM"},
        ),
    }
    for environment, (endpoint, names) in groups.items():
        try:
            data = github.get(endpoint + "?per_page=100")
            if data.get("total_count", 101) > 100:
                raise ValueError
            existing = {row["name"] for row in data["secrets"]}
            result[environment] = {
                name: "PRESENT_VALUE_UNVERIFIED_DO_NOT_OVERWRITE" if name in existing else "MISSING"
                for name in sorted(names)
            }
        except Exception:
            result[environment] = {
                name: "UNKNOWN_ACCESS_OR_RESPONSE_UNVERIFIED" for name in sorted(names)
            }
    try:
        variable = github.get(
            "environments/nonprod-database/variables/NONPROD_DATABASE_EVIDENCE_KEY_ID"
        )
        result["database_key_id_variable"] = variable["value"]
    except Exception:
        result["database_key_id_variable"] = "UNKNOWN_OR_MISSING"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=[*DESTINATIONS, "inventory"])
    parser.add_argument("--session")
    parser.add_argument("--expires-at", help="Explicit ISO-8601 expiry including timezone")
    parser.add_argument("--review-pr", type=int)
    parser.add_argument(
        "--apply-operation", choices=["foundation-apply", "migration-apply", "runtime-apply"]
    )
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--database-key-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        operator_only()
        github = ReadOnlyGithub()
        if args.kind == "inventory":
            print(json.dumps(enrollment_inventory(github), indent=2))
            return
        if not args.session or not args.expires_at or not args.review_pr or not args.output:
            raise AdmissionDenied("EXPLICIT_SESSION_EXPIRY_REVIEW_AND_OUTPUT_REQUIRED")
        expiry = datetime.fromisoformat(args.expires_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise AdmissionDenied("EXPIRY_TIMEZONE_REQUIRED")
        request = prepare(
            args.kind,
            session=args.session,
            expires_at=int(expiry.timestamp()),
            review_pr=args.review_pr,
            github=github,
            runner=Runner(),
            config=configuration(),
            apply_operation=args.apply_operation,
            plan=args.plan,
            artifact_id=args.artifact_id,
            database_key_id=args.database_key_id,
        )
        if args.kind == "apply":
            if not sys.stdin.isatty():
                raise AdmissionDenied("INTERACTIVE_PLAN_REVIEW_REQUIRED")
            print(json.dumps(request["plan_review"], indent=2))
            print("Review this exact saved plan using terraform show before confirming.")
            expected = "REVIEW " + request["plan_review"]["plan_sha256"]
            if input("Type REVIEW followed by the displayed plan SHA-256: ") != expected:
                raise AdmissionDenied("EXACT_PLAN_REVIEW_NOT_CONFIRMED")
            request["plan_review"]["reviewed_by_operator"] = True
        parent = args.output.absolute().parent
        if not parent.exists():
            private_directory(parent, create=True)
        private_write(args.output, json.dumps(request, sort_keys=True).encode())
        print(
            json.dumps(
                {
                    "status": "UNSIGNED_REQUEST_CAPTURED",
                    "reviewed_request_sha256": digest(request),
                    "commit": request["commit"],
                    "operations": request["operations"],
                    "destination": request["destination"],
                    "expires_at": request["expires_at"],
                    "provisioning_authorized": False,
                },
                indent=2,
            )
        )
    except AdmissionDenied as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit(
            "RELEASE_CAPTURE_BLOCKED: evidence unverified; no authorization issued"
        ) from None


if __name__ == "__main__":
    main()
