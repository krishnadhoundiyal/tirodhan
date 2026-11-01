"""Explicit operator enrollment, independent of vault existence and recovery itself."""

from __future__ import annotations

import json
import uuid
from typing import Any

from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.foundation_reconcile import admitted
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.lifecycle_evidence import Observations

DISCOVERY = [
    "Microsoft.Resources/subscriptions/resourceGroups/read",
    "Microsoft.KeyVault/deletedVaults/read",
    "Microsoft.KeyVault/locations/deletedVaults/read",
    "Microsoft.KeyVault/locations/operationResults/read",
]
WRITE = [
    "Microsoft.KeyVault/vaults/read",
    "Microsoft.KeyVault/vaults/write",
    "Microsoft.Authorization/permissions/read",
]
FEDERATION = {
    "name": "tirodhan-nonprod-recovery",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:krishnadhoundiyal@46423210/tirodhan@1385964140:environment:nonprod-recovery",
    "audiences": ["api://AzureADTokenExchange"],
}


def federation(io: Observations, app: str) -> dict[str, Any] | None:
    rows = io.runner.json(
        "az",
        "rest",
        "--method",
        "get",
        "--url",
        f"https://graph.microsoft.com/v1.0/applications/{app}/federatedIdentityCredentials",
    )
    if rows.get("@odata.nextLink") or not isinstance(rows.get("value"), list):
        raise AdmissionDenied("RECOVERY_FEDERATION_DISCOVERY_UNVERIFIED")
    if any(row.get("name") != FEDERATION["name"] for row in rows["value"]):
        raise AdmissionDenied("RECOVERY_FEDERATION_HAS_UNAPPROVED_TRUST")
    matches = [r for r in rows["value"] if r.get("name") == FEDERATION["name"]]
    if len(matches) > 1 or (matches and any(matches[0].get(k) != v for k, v in FEDERATION.items())):
        raise AdmissionDenied("RECOVERY_FEDERATION_REBIND_NOT_AUTHORIZED")
    return matches[0] if matches else None


def verify_application(checks: Checks) -> tuple[str, str]:
    binding, io = checks.binding(), checks.io
    try:
        app = str(uuid.UUID(binding["recovery_app_object_id"]))
        principal = str(uuid.UUID(binding["recovery_principal_id"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise AdmissionDenied("RECOVERY_ENROLLMENT_TARGET_INVALID") from exc
    if principal.casefold() == checks.auth.principal_id.casefold():
        raise AdmissionDenied("RECOVERY_ENROLLMENT_REQUIRES_SEPARATE_OPERATOR")
    sp = io.runner.json("az", "ad", "sp", "show", "--id", principal)
    application = io.runner.json("az", "ad", "app", "show", "--id", app)
    owners = io.runner.json("az", "ad", "app", "owner", "list", "--id", app)
    client = str(checks.config.get("recovery_client_id", "")).casefold()
    if (
        not client
        or client == checks.config["deployment_client_id"].casefold()
        or str(sp.get("id", "")).casefold() != principal
        or str(sp.get("appId", "")).casefold() != client
        or str(application.get("id", "")).casefold() != app
        or str(application.get("appId", "")).casefold() != client
        or not any(
            str(row.get("id", "")).casefold() == checks.auth.principal_id.casefold()
            for row in owners
        )
    ):
        raise AdmissionDenied("RECOVERY_ENROLLMENT_IDENTITY_OR_OWNERSHIP_UNVERIFIED")
    federation(io, app)  # Refuse incompatible existing trust before any writes.
    return app, principal


def role(io: Observations, name: str, scope: str, actions: list[str]) -> str:
    expected = {
        "roleName": name,
        "type": "CustomRole",
        "description": "Explicit NONPROD recovery enrollment",
        "assignableScopes": [scope],
        "permissions": [
            {"actions": actions, "notActions": [], "dataActions": [], "notDataActions": []}
        ],
    }
    existing = io.runner.json(
        "az", "role", "definition", "list", "--name", name, "--scope", scope, "-o", "json"
    )
    if len(existing) > 1:
        raise AdmissionDenied("RECOVERY_ENROLLMENT_ROLE_AMBIGUOUS")
    if existing:
        row = existing[0]
        if row.get("permissions") != expected["permissions"] or row.get("assignableScopes") != [
            scope
        ]:
            raise AdmissionDenied("RECOVERY_ENROLLMENT_ROLE_CHANGE_NOT_AUTHORIZED")
        return str(row["id"])
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, scope + "/" + name))
    resource_id = f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{identifier}"
    value = io.runner.json(
        "az",
        "rest",
        "--method",
        "put",
        "--url",
        "https://management.azure.com" + resource_id + "?api-version=2022-04-01",
        "--body",
        json.dumps({"properties": expected}),
    )
    properties = value.get("properties", {})
    if (
        properties.get("permissions") != expected["permissions"]
        or properties.get("assignableScopes") != [scope]
        or value.get("name") != identifier
        or not isinstance(value.get("id"), str)
    ):
        raise AdmissionDenied("RECOVERY_ENROLLMENT_ROLE_POSTCONDITION_FAILED")
    return str(value["id"])


def enroll() -> None:
    checks, io = admitted("enroll-recovery")
    app, principal = verify_application(checks)
    subscription = "/subscriptions/" + io.tf["subscription_id"]
    for name, scope, actions in (
        (f"Tirodhan NONPROD Deleted Vault Discovery {io.tf['suffix']}", subscription, DISCOVERY),
        (f"Tirodhan NONPROD Vault Recovery Write {io.tf['suffix']}", io.scope, WRITE),
    ):
        definition = role(io, name, scope, actions)
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join([name, scope, principal])))
        target = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{identifier}"
        expected = {
            "roleDefinitionId": definition,
            "principalId": principal,
            "principalType": "ServicePrincipal",
        }
        value = io.runner.json(
            "az",
            "rest",
            "--method",
            "put",
            "--url",
            "https://management.azure.com" + target + "?api-version=2022-04-01",
            "--body",
            json.dumps({"properties": expected}),
        )
        properties = value.get("properties", {})
        if (
            str(value.get("id", "")).casefold() != target.casefold()
            or any(
                str(properties.get(k, "")).casefold() != v.casefold() for k, v in expected.items()
            )
            or properties.get("condition")
        ):
            raise AdmissionDenied("RECOVERY_ENROLLMENT_ASSIGNMENT_POSTCONDITION_FAILED")
    if federation(io, app) is None:
        try:
            io.runner.run(
                "az",
                "rest",
                "--method",
                "post",
                "--url",
                f"https://graph.microsoft.com/v1.0/applications/{app}/federatedIdentityCredentials",
                "--body",
                json.dumps(FEDERATION),
                "--output",
                "none",
            )
        except AdmissionDenied:
            if federation(io, app) is None:
                raise  # Unknown outcome remains failure; uniqueness protects a later replay.
    if federation(io, app) is None:
        raise AdmissionDenied("RECOVERY_ENROLLMENT_FEDERATION_POSTCONDITION_FAILED")
    print("RECOVERY_IDENTITY_ENROLLED: separately authorize recovery and reevaluate prerequisites")
