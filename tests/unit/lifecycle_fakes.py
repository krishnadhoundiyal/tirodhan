"""Functional admission, adversarial evidence, state classification and effect boundaries."""

from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path
from typing import Any

from tirodhan.deployment.check_nonprod_bootstrap_rbac import (
    ADMIN_ROLE,
    BOOTSTRAP_ROLE_NAMES,
    expected_admin_condition,
)
from tirodhan.deployment.deployment_admission import (
    Authorization,
    digest,
)
from tirodhan.deployment.lifecycle_evidence import (
    BackendState,
    Observations,
)

SUB = "00000000-0000-0000-0000-000000000001"


OID = "00000000-0000-0000-0000-000000000002"


CLIENT = "00000000-0000-0000-0000-000000000003"


TENANT = "00000000-0000-0000-0000-000000000004"


SCOPE = f"/subscriptions/{SUB}/resourceGroups/tirodhan-np-test01"


VAULT = SCOPE + "/providers/Microsoft.KeyVault/vaults/td-np-test01-kv"


RESOURCE = SCOPE + "/providers/Microsoft.App/managedEnvironments/test-aca"


CONFIG = {
    "terraform": {
        "subscription_id": SUB,
        "tenant_id": TENANT,
        "resource_group_name": "tirodhan-np-test01",
        "location": "centralindia",
        "suffix": "test01",
        "runtime_env": {"TIRODHAN_OTP_PROVIDER": "2FACTOR"},
    },
    "state_account": "syntheticstate",
    "state_resource_group": "synthetic-state-rg",
    "deployment_client_id": CLIENT,
    "recovery_client_id": "separate-client",
}


def state(resources: dict[str, str] | None = None, *, serial: int = 0) -> BackendState:
    values = resources or {}
    document = {
        "version": 4,
        "lineage": "lineage",
        "serial": serial,
        "resources": [
            {
                "mode": "managed",
                "type": key.split(".")[0],
                "name": key.split(".")[1],
                "instances": [{"attributes": {"id": value}}],
            }
            for key, value in values.items()
        ],
        "outputs": {},
    }
    return BackendState("INITIALIZED", document, f"etag-{serial}", values)


def auth(operation: str, *, kind: str = "deployment", **binding: Any) -> Authorization:
    return Authorization(
        operation,
        OID,
        kind,
        digest(CONFIG),
        "code-hash",
        "a" * 40,
        "session",
        {"bindings": {operation: {"local_execution_authorized": True, **binding}}},
    )


def write_plan(path: Path, prior: BackendState) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("tfstate", json.dumps(prior.document))
        archive.writestr("tfstate-prev", json.dumps({"lineage": "", "serial": 0}))
        archive.writestr("tfplan", b"synthetic native plan payload")


def assignments() -> list[dict[str, Any]]:
    return [
        {
            "scope": SCOPE,
            "principalId": OID,
            "roleDefinitionName": name,
            "condition": expected_admin_condition() if name == ADMIN_ROLE else None,
            "conditionVersion": "2.0" if name == ADMIN_ROLE else None,
        }
        for name in (*BOOTSTRAP_ROLE_NAMES, "Tirodhan NONPROD Deployment test01")
    ]


class MemoryRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.plan: dict[str, Any] = {
            "variables": {
                **{k: {"value": v} for k, v in CONFIG["terraform"].items()},
                "deployment_stage": {"value": 0},
            },
            "resource_changes": [],
        }

    def json(self, *args: str) -> Any:
        self.commands.append(args)
        if args[:4] == ("az", "role", "assignment", "list"):
            return assignments()
        if args[:4] == ("az", "postgres", "flexible-server", "list-skus"):
            return [{"name": "Standard_B1ms"}]
        if args == ("terraform", "version", "-json"):
            return {
                "terraform_version": "1.13.5",
                "provider_selections": {"registry.terraform.io/hashicorp/azurerm": "5.8.0"},
            }
        if args[:3] == ("terraform", "show", "-json"):
            return self.plan
        raise AssertionError(args)

    def run(self, *args: str, **_: Any) -> str:
        self.commands.append(args)
        raise AssertionError("No mutation was expected: " + args[0])


class MemoryObservations(Observations):
    def __init__(self) -> None:
        super().__init__(copy.deepcopy(CONFIG))
        self.runner = MemoryRunner()  # type: ignore[assignment]
        self.current = state()
        self.active = {VAULT}
        self.vault_present = True
        self.inputs_verified = True

    def terraform_context(self) -> dict[str, Any]:
        return {
            "type": "azurerm",
            "config": {
                "storage_account_name": self.config["state_account"],
                "resource_group_name": self.config["state_resource_group"],
                "container_name": "tfstate",
                "key": "tirodhan/nonprod.tfstate",
                "use_azuread_auth": True,
            },
        }

    def terraform_inputs(self) -> bool:
        return self.inputs_verified

    def identity(self) -> dict[str, Any]:
        return {"oid": OID, "appid": CLIENT, "tid": TENANT}

    def backend(self) -> BackendState:
        return self.current

    def inventory(self) -> set[str]:
        return self.active

    def arm(self, resource_id: str, version: str) -> dict[str, Any] | None:
        if resource_id.endswith("/providers/Microsoft.Authorization/permissions"):
            return {"value": [{"actions": ["*/read"], "notActions": []}]}
        if "/providers/Microsoft." in resource_id and resource_id.endswith(
            (
                "Microsoft.App",
                "Microsoft.Storage",
                "Microsoft.KeyVault",
                "Microsoft.ServiceBus",
                "Microsoft.ManagedIdentity",
                "Microsoft.DBforPostgreSQL",
            )
        ):
            return {"registrationState": "Registered"}
        if resource_id == VAULT and self.vault_present:
            return {
                "id": VAULT,
                "location": "centralindia",
                "properties": {
                    "tenantId": TENANT,
                    "enablePurgeProtection": True,
                    "enableRbacAuthorization": True,
                    "provisioningState": "Succeeded",
                },
            }
        return None
