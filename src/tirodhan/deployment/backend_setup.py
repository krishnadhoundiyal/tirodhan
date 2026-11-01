"""Admission before native backend access, which may implicitly create remote state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tirodhan.deployment.deployment_admission import (
    ROOT,
    AdmissionDenied,
    configuration,
    require_release_authorization,
)
from tirodhan.deployment.lifecycle_evidence import Observations


def terraform_environment_safe() -> bool:
    """Ambient arguments/workspaces must not bypass target selection or native locks."""
    if any(
        value
        for key, value in os.environ.items()
        if key.startswith("TF_CLI_ARGS")
        or key
        in {
            "TF_DATA_DIR",
            "TF_CLI_CONFIG_FILE",
            "ARM_CLIENT_SECRET",
            "ARM_ACCESS_KEY",
            "ARM_SAS_TOKEN",
        }
    ):
        return False
    if os.environ.get("TF_WORKSPACE", "default") != "default":
        return False
    if os.environ.get("ARM_USE_MSI", "false").lower() != "false":
        return False
    return True


def initialization_context(io: Observations) -> bool:
    """Safe before init: version reads local metadata and never opens remote state."""
    if Path.cwd().resolve() != (ROOT / "infra/terraform/nonprod").resolve():
        return False
    if any(Path(name).exists() for name in ("terraform.tfstate", "terraform.tfstate.backup")):
        return False  # No local state may be implicitly migrated into the Azure backend.
    if not terraform_environment_safe():
        return False
    if io.terraform_inputs() is not True:
        return False
    return bool(
        io.runner.json("terraform", "version", "-json").get("terraform_version") == "1.13.5"
    )


def intent(checks: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "purpose": "initialize-empty-nonprod-backend",
        "configuration_hash": checks.auth.configuration_hash,
        "contract_hash": checks.auth.contract_hash,
        "commit": checks.auth.commit,
        "release_session": checks.auth.run_id,
        "principal_id": checks.auth.principal_id,
        "state_account_id": checks.io.state_id,
        "container": "tfstate",
        "key": "tirodhan/nonprod.tfstate",
        "retained_vault_id": checks.io.vault_id,
        "before_state_fingerprint": checks.backend().fingerprint,
        "managed_ids": [],
        "disposable_ids": [],
    }


def validate_intent(checks: Any, path: Any) -> None:
    if not isinstance(path, Path) or not path.is_file() or path.stat().st_size > 16384:
        raise AdmissionDenied("INITIALIZATION_INTENT_UNAVAILABLE")
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise AdmissionDenied("INITIALIZATION_INTENT_INVALID") from exc
    binding = checks.binding()
    if (
        hashlib.sha256(raw).hexdigest() != binding.get("intent_sha256")
        or document != intent(checks)
        or binding.get("state_fingerprint") != checks.backend().fingerprint
    ):
        raise AdmissionDenied("INITIALIZATION_INTENT_MISMATCH")


def native(io: Observations, *arguments: str, data_dir: str | None = None) -> None:
    environment = dict(os.environ)
    if data_dir is not None:
        environment["TF_DATA_DIR"] = data_dir
    environment["TF_WORKSPACE"] = "default"
    try:
        result = subprocess.run(
            ["terraform", *arguments],
            env=environment,
            capture_output=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdmissionDenied("BACKEND_SETUP_OUTCOME_UNVERIFIED") from exc
    if result.returncode:
        # State pull output and all provider/CLI diagnostics remain private.
        raise AdmissionDenied("BACKEND_SETUP_OUTCOME_UNVERIFIED")


def init_arguments(io: Observations) -> tuple[str, ...]:
    return (
        "init",
        "-reconfigure",
        "-input=false",
        "-lockfile=readonly",
        f"-backend-config=storage_account_name={io.config['state_account']}",
        f"-backend-config=resource_group_name={io.config['state_resource_group']}",
        "-backend-config=container_name=tfstate",
        "-backend-config=key=tirodhan/nonprod.tfstate",
        "-backend-config=use_azuread_auth=true",
    )


def setup(mode: str) -> None:
    if mode == "provisioning":
        from tirodhan.deployment.foundation_reconcile import admitted

        checks, io = admitted("terraform-setup")
        if not initialization_context(io):
            raise AdmissionDenied("BACKEND_SETUP_CONTEXT_INVALID")
        # A second observation immediately before native access rejects changed state.
        if io.backend().fingerprint != checks.backend().fingerprint:
            raise AdmissionDenied("BACKEND_CHANGED_BEFORE_SETUP")
    elif mode == "destroy":
        # Teardown retains its independent protected workflow, including initialized-empty
        # legacy cases. It never obtains initialization or application deployment authority.
        if (
            os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_WORKFLOW_REF")
            != "krishnadhoundiyal/tirodhan/.github/workflows/nonprod-destroy.yml@refs/heads/main"
            or os.environ.get("TIRODHAN_EXECUTION_ENVIRONMENT") not in {"nonprod", "nonprod-plan"}
        ):
            raise AdmissionDenied("DESTROY_SETUP_CONTEXT_INVALID")
        io = Observations(configuration())
        claims = io.identity()
        if (
            str(claims.get("appid") or claims.get("azp")).casefold()
            != io.config["deployment_client_id"].casefold()
            or io.backend().kind != "INITIALIZED"
            or not initialization_context(io)
        ):
            raise AdmissionDenied("DESTROY_BACKEND_NOT_INITIALIZED")
    else:
        raise AdmissionDenied("UNKNOWN_BACKEND_SETUP_MODE")
    native(io, *init_arguments(io))
    if io.backend().kind != "INITIALIZED":
        raise AdmissionDenied("BACKEND_SETUP_POSTCONDITIONS_FAILED")


def initialize(operation: str, path: Path) -> None:
    from tirodhan.deployment.foundation_lifecycle import verify_clean, verify_preserved_permissions
    from tirodhan.deployment.foundation_reconcile import admitted

    checks, io = admitted(operation, plan=path)
    if operation == "initialize-state-plan":
        # No native plan/state command: StateMgr itself can persist an empty state.
        with path.open("x", encoding="utf-8") as output:
            json.dump(intent(checks), output, sort_keys=True, indent=2)
        print("INITIALIZATION_INTENT_PREPARED: no remote mutation")
        return
    if operation != "initialize-state-apply":
        raise AdmissionDenied("UNKNOWN_INITIALIZATION_OPERATION")
    if require_release_authorization(operation) != checks.auth:
        raise AdmissionDenied("INITIALIZATION_AUTHORIZATION_CHANGED")
    if io.backend().fingerprint != checks.backend().fingerprint:
        raise AdmissionDenied("BACKEND_CHANGED_BEFORE_INITIALIZATION")
    validate_intent(checks, path)
    with tempfile.TemporaryDirectory(prefix="tirodhan-backend-init-") as data_dir:
        # No stale local state/backend is ever migrated. Terraform creates its own empty
        # state with its native Azure lease protocol; we never upload synthetic state.
        native(io, *init_arguments(io), data_dir=data_dir)
        native(io, "state", "pull", data_dir=data_dir)
    verify_clean(io)
    verify_preserved_permissions(checks)
    print("BACKEND_INITIALIZATION_VERIFIED: reevaluate deployment prerequisites")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["provisioning", "destroy"])
    args = parser.parse_args()
    try:
        setup(args.mode)
    except (AdmissionDenied, OSError, ValueError, KeyError):
        raise SystemExit("BACKEND_SETUP_BLOCKED") from None


if __name__ == "__main__":
    main()
