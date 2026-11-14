"""Bounded, sanitized observations. A denied read is never classified as absence."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tirodhan.deployment.deployment_admission import ROOT, AdmissionDenied, digest


class ObservationError(AdmissionDenied):
    pass


def azure_error_code(stderr: str) -> str:
    for pattern in (
        r"\(([A-Za-z][A-Za-z0-9]{1,63})\)",
        r'"code"\s*:\s*"([A-Za-z][A-Za-z0-9]{1,63})"',
        r"ErrorCode:\s*([A-Za-z][A-Za-z0-9]{1,63})\b",
    ):
        match = re.search(pattern, stderr)
        if match:
            return match.group(1)
    return "COMMAND_FAILED"


class Runner:
    timeout_limit: int | None = None

    def run(self, *args: str, timeout: int = 180) -> str:
        try:
            executable = shutil.which(args[0]) or args[0]
            command: tuple[str, ...] = (executable,)
            if os.name == "nt" and executable.casefold().endswith((".cmd", ".bat")):
                # Invoke the official MSI's Python entry point directly. Windows
                # may interpret batch arguments through cmd.exe even with shell=False.
                launcher = Path(executable)
                interpreter = launcher.parent.parent / "python.exe"
                if (
                    launcher.name.casefold() != "az.cmd"
                    or launcher.parent.name.casefold() != "wbin"
                    or not interpreter.is_file()
                ):
                    raise ObservationError("UNSUPPORTED_BATCH_LAUNCHER")
                command = (str(interpreter), "-IBm", "azure.cli")
            result = subprocess.run(
                (*command, *args[1:]),
                check=False,
                capture_output=True,
                text=True,
                timeout=min(timeout, self.timeout_limit)
                if self.timeout_limit is not None
                else timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ObservationError("OBSERVATION_UNAVAILABLE") from exc
        if result.returncode:
            # Do not emit command arguments, stderr, Terraform state or provider bodies.
            raise ObservationError(azure_error_code(result.stderr))
        return result.stdout.strip()

    def json(self, *args: str) -> Any:
        try:
            return json.loads(self.run(*args))
        except (ValueError, TypeError) as exc:
            raise ObservationError("MALFORMED_OBSERVATION") from exc


def state_resources(state: dict[str, Any]) -> dict[str, str]:
    """Inspect raw backend state, including modules, rather than Terraform outputs."""
    if state.get("version") != 4 or not isinstance(state.get("resources"), list):
        raise ObservationError("UNSUPPORTED_STATE_FORMAT")
    if (
        not isinstance(state.get("lineage"), str)
        or not state["lineage"].strip()
        or type(state.get("serial")) is not int
        or state["serial"] < 0
    ):
        raise ObservationError("INVALID_STATE_IDENTITY")
    result: dict[str, str] = {}
    for row in state["resources"]:
        if row.get("mode") == "data":
            continue
        if row.get("mode") != "managed":
            raise ObservationError("UNKNOWN_STATE_RESOURCE_MODE")
        address = ".".join(filter(None, [row.get("module"), row["type"], row["name"]]))
        for instance in row.get("instances", []):
            if instance.get("deposed"):
                raise ObservationError("DEPOSED_STATE_REQUIRES_RECONCILIATION")
            suffix = ""
            if "index_key" in instance:
                suffix = "[" + json.dumps(instance["index_key"]) + "]"
            resource_id = instance.get("attributes", {}).get("id")
            if not isinstance(resource_id, str) or not resource_id:
                raise ObservationError("STATE_RESOURCE_ID_UNAVAILABLE")
            result[address + suffix] = resource_id
    return result


@dataclass(frozen=True)
class BackendState:
    kind: str
    document: dict[str, Any] | None
    etag: str | None
    resources: dict[str, str]

    @property
    def fingerprint(self) -> str:
        return digest({"kind": self.kind, "etag": self.etag, "state": self.document})


def classify_state(
    backend: BackendState, active: set[str], *, app_scope: str, retained: set[str]
) -> str:
    scope = app_scope.casefold() + "/"
    if any(not item.casefold().startswith(scope) for item in active):
        raise ObservationError("OUT_OF_SCOPE_INVENTORY")
    disposable = {item.casefold() for item in active} - {i.casefold() for i in retained}
    owned = {item.casefold() for item in backend.resources.values()}
    if any(not item.startswith(scope) for item in owned):
        raise ObservationError("OUT_OF_SCOPE_STATE")
    if backend.kind in {"BACKEND_ABSENT", "STATE_ABSENT"}:
        return "CONFLICT" if disposable or owned else backend.kind
    if backend.kind != "INITIALIZED":
        raise ObservationError("UNKNOWN_BACKEND_STATE")
    if not owned:
        return "CONFLICT" if disposable else "CLEAN_INITIALIZED"
    if disposable - owned:
        return "CONFLICT"
    # A missing owned Azure resource is drift, never a new clean baseline.
    return "PARTIAL_OWNED" if owned - disposable else "ESTABLISHED_OWNED"


def plan_prior_state(path: Path) -> dict[str, Any]:
    """Read Terraform's refreshed prior-state identity used by its native stale-plan fence."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            # tfstate-prev deliberately has a stub header. Terraform 1.13.5 stores
            # PlannedStateUpdate lineage/serial in tfstate (backend_plan.go:140-156).
            info = archive.getinfo("tfstate")
            if info.file_size > 32 * 1024 * 1024:
                raise ObservationError("PLAN_STATE_TOO_LARGE")
            value = json.loads(archive.read(info))
            if not isinstance(value, dict):
                raise ValueError
            return value
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise ObservationError("PLAN_STATE_IDENTITY_UNAVAILABLE") from exc


class Observations:
    def __init__(self, config: dict[str, Any], runner: Runner | None = None) -> None:
        self.config = config
        self.tf = config["terraform"]
        self.runner = runner or Runner()
        self.scope = (
            f"/subscriptions/{self.tf['subscription_id']}"
            f"/resourceGroups/{self.tf['resource_group_name']}"
        )
        self.state_id = (
            f"/subscriptions/{self.tf['subscription_id']}"
            f"/resourceGroups/{config['state_resource_group']}"
            f"/providers/Microsoft.Storage/storageAccounts/{config['state_account']}"
        )
        self.vault_id = (
            self.scope + f"/providers/Microsoft.KeyVault/vaults/td-np-{self.tf['suffix']}-kv"
        )

    def arm(self, resource_id: str, version: str) -> dict[str, Any] | None:
        try:
            value = self.runner.json(
                "az",
                "rest",
                "--method",
                "get",
                "--url",
                f"https://management.azure.com{resource_id}?api-version={version}",
                "--output",
                "json",
            )
        except ObservationError as exc:
            if str(exc) in {"ResourceNotFound", "ResourceGroupNotFound"}:
                return None
            if str(exc) == "ServiceNotFound" and re.fullmatch(
                r"/subscriptions/[^/]+/providers/microsoft\.apimanagement/"
                r"locations/[^/]+/deletedservices/[^/]+",
                resource_id.casefold(),
            ):
                return None
            if str(exc) == "RoleAssignmentNotFound" and re.search(
                r"/providers/microsoft\.authorization/roleassignments/[^/]+$",
                resource_id.casefold(),
            ):
                return None
            raise
        if not isinstance(value, dict):
            raise ObservationError("MALFORMED_ARM_RESOURCE")
        return value

    def identity(self) -> dict[str, Any]:
        account = self.runner.json("az", "account", "show", "--output", "json")
        if (
            str(account.get("id", "")).casefold() != self.tf["subscription_id"].casefold()
            or str(account.get("tenantId", "")).casefold() != self.tf["tenant_id"].casefold()
        ):
            raise ObservationError("AUTHENTICATED_TARGET_MISMATCH")
        token = self.runner.run(
            "az",
            "account",
            "get-access-token",
            "--resource",
            "https://management.azure.com/",
            "--query",
            "accessToken",
            "--output",
            "tsv",
        )
        try:
            payload = token.split(".")[1]
            claims: dict[str, Any] = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
        except (ValueError, IndexError, TypeError) as exc:
            raise ObservationError("IDENTITY_UNAVAILABLE") from exc
        if str(claims.get("tid", "")).casefold() != self.tf["tenant_id"].casefold():
            raise ObservationError("IDENTITY_TENANT_MISMATCH")
        return claims

    def terraform_context(self) -> dict[str, Any]:
        expected = (ROOT / "infra/terraform/nonprod").resolve()
        if Path.cwd().resolve() != expected:
            raise ObservationError("TERRAFORM_PROJECT_CONTEXT_MISMATCH")
        data = Path(os.environ.get("TF_DATA_DIR", str(expected / ".terraform")))
        workspace = data / "environment"
        if workspace.exists() and workspace.read_text(encoding="utf-8").strip() != "default":
            raise ObservationError("TERRAFORM_WORKSPACE_CONTEXT_MISMATCH")
        value = json.loads((data / "terraform.tfstate").read_text(encoding="utf-8-sig"))
        return dict(value.get("backend", {}))

    def terraform_inputs(self) -> bool:
        """Only the authoritative generated input file and release-owned stage are ambient."""
        expected = (ROOT / "infra/terraform/nonprod").resolve()
        if Path.cwd().resolve() != expected:
            raise ObservationError("TERRAFORM_PROJECT_CONTEXT_MISMATCH")
        approved = expected / "nonprod.auto.tfvars.json"
        files = set(expected.glob("*.auto.tfvars")) | set(expected.glob("*.auto.tfvars.json"))
        files |= {
            p
            for p in (expected / "terraform.tfvars", expected / "terraform.tfvars.json")
            if p.exists()
        }
        if files != {approved} or json.loads(approved.read_text(encoding="utf-8-sig")) != self.tf:
            raise ObservationError("TERRAFORM_INPUTS_NOT_AUTHORITATIVE")
        if any(
            key.startswith("TF_VAR_") and key != "TF_VAR_deployment_stage" for key in os.environ
        ):
            raise ObservationError("TERRAFORM_AMBIENT_VARIABLE_OVERRIDE")
        return True

    def inventory(self) -> set[str]:
        rows = self.runner.json(
            "az",
            "resource",
            "list",
            "--resource-group",
            self.tf["resource_group_name"],
            "--subscription",
            self.tf["subscription_id"],
            "--output",
            "json",
        )
        if not isinstance(rows, list) or any(not isinstance(r.get("id"), str) for r in rows):
            raise ObservationError("INVENTORY_UNAVAILABLE")
        return {row["id"] for row in rows}

    def backend(self) -> BackendState:
        account = self.arm(self.state_id, "2025-06-01")
        if account is None:
            return BackendState("BACKEND_ABSENT", None, None, {})
        properties = account.get("properties", {})
        if (
            str(account.get("id", "")).casefold() != self.state_id.casefold()
            or properties.get("allowSharedKeyAccess") is not False
            or properties.get("allowBlobPublicAccess") is not False
        ):
            raise ObservationError("BACKEND_IDENTITY_OR_SECURITY_MISMATCH")
        container = self.runner.json(
            "az",
            "storage",
            "container",
            "show",
            "--account-name",
            self.config["state_account"],
            "--name",
            "tfstate",
            "--auth-mode",
            "login",
            "--output",
            "json",
        )
        if container.get("properties", {}).get("publicAccess") not in (None, "None"):
            raise ObservationError("BACKEND_CONTAINER_NOT_PRIVATE")
        args = (
            "--account-name",
            self.config["state_account"],
            "--container-name",
            "tfstate",
            "--name",
            "tirodhan/nonprod.tfstate",
            "--auth-mode",
            "login",
        )
        try:
            metadata = self.runner.json("az", "storage", "blob", "show", *args, "-o", "json")
        except ObservationError as exc:
            if str(exc) == "BlobNotFound":
                return BackendState("STATE_ABSENT", None, None, {})
            raise
        etag = metadata.get("properties", {}).get("etag")
        size = metadata.get("properties", {}).get("contentLength")
        if not isinstance(etag, str) or type(size) is not int or not 0 < size <= 32 * 1024 * 1024:
            raise ObservationError("STATE_METADATA_UNAVAILABLE")
        with tempfile.TemporaryDirectory(prefix="tirodhan-state-observation-") as temporary:
            path = Path(temporary) / "state.json"
            self.runner.run(
                "az",
                "storage",
                "blob",
                "download",
                *args,
                "--file",
                str(path),
                "--if-match",
                etag,
                "--no-progress",
                "--output",
                "none",
            )
            try:
                state = json.loads(path.read_text(encoding="utf-8-sig"))
                resources = state_resources(state)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                raise ObservationError("INVALID_REMOTE_STATE") from exc
        return BackendState("INITIALIZED", state, etag, resources)


def environment_scope() -> str:
    return os.environ["DEPLOYMENT_SCOPE"]
