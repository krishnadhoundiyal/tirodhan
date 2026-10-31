"""Fail-closed verification of NONPROD bootstrap RBAC after Terraform teardown."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import Any

BOOTSTRAP_ROLES = (
    "69a216fc-b8fb-44d8-bc22-1f3c2cd27a39",
    "4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0",
    "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
    "db58b8e5-c6ad-4a2a-8342-4190687cbf4a",
    "4633458b-17de-408a-b874-0445c86b69e6",
)
ADMIN_ROLE = "Role Based Access Control Administrator"
BOOTSTRAP_ROLE_NAMES = (
    "Storage Blob Data Contributor",
    "Storage Blob Delegator",
    "Key Vault Secrets Officer",
    ADMIN_ROLE,
)


def expected_admin_condition() -> str:
    """Mirror the conditional RBAC delegation in deploy/bootstrap/bootstrap.sh."""
    roles = "{" + ", ".join(BOOTSTRAP_ROLES) + "}"
    return (
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR "
        "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
        f"ForAnyOfAnyValues:GuidEquals {roles})) AND "
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR "
        "(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
        f"ForAnyOfAnyValues:GuidEquals {roles}))"
    )


def principal_from_arm_token(token: str, *, client_id: str, tenant_id: str) -> str:
    """Read identity claims from Azure CLI's ARM token, without logging the token."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ValueError("Azure CLI did not provide a JWT access token")
    try:
        payload = parts[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("Unable to decode Azure CLI identity claims") from error
    if not isinstance(claims, dict):
        raise ValueError("Azure CLI identity claims are invalid")
    identity = claims.get("appid") or claims.get("azp")
    principal_id = claims.get("oid")
    if (
        not isinstance(identity, str)
        or identity.casefold() != client_id.casefold()
        or not isinstance(claims.get("tid"), str)
        or claims["tid"].casefold() != tenant_id.casefold()
        or not isinstance(principal_id, str)
        or not principal_id.strip()
    ):
        raise ValueError("Authenticated deployment identity does not match configured tenant/client")
    return principal_id


def verify_bootstrap_rbac(
    assignments: list[dict[str, Any]],
    *,
    expected_scope: str,
    expected_principal_id: str,
    expected_suffix: str,
) -> None:
    """Accept only the five original RG grants to the authenticated deployment SP."""
    names = (*BOOTSTRAP_ROLE_NAMES, f"Tirodhan NONPROD Deployment {expected_suffix}")
    if not isinstance(assignments, list) or len(assignments) != len(names):
        raise ValueError("NONPROD RG must have exactly five direct bootstrap role assignments")
    if any(not isinstance(row, dict) for row in assignments):
        raise ValueError("Malformed NONPROD RG role assignment")
    by_name = {row.get("roleDefinitionName"): row for row in assignments}
    if len(by_name) != len(names) or set(by_name) != set(names):
        raise ValueError("NONPROD RG bootstrap roles differ from the expected grant set")
    for row in assignments:
        scope = row.get("scope")
        principal = row.get("principalId")
        if (
            not isinstance(scope, str)
            or scope.casefold() != expected_scope.casefold()
            or not isinstance(principal, str)
            or principal.casefold() != expected_principal_id.casefold()
        ):
            raise ValueError("NONPROD bootstrap RBAC scope or principal does not match deployment identity")
    administrator = by_name[ADMIN_ROLE]
    condition = administrator.get("condition")
    if (
        administrator.get("conditionVersion") != "2.0"
        or not isinstance(condition, str)
        or " ".join(condition.split()) != " ".join(expected_admin_condition().split())
    ):
        raise ValueError("NONPROD RBAC administrator delegation condition is missing or weakened")


def main() -> None:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="action", required=True)
    principal = subcommands.add_parser("principal")
    principal.add_argument("--client-id", required=True)
    principal.add_argument("--tenant-id", required=True)
    verify = subcommands.add_parser("verify")
    verify.add_argument("--expected-scope", required=True)
    verify.add_argument("--expected-principal-id", required=True)
    verify.add_argument("--expected-suffix", required=True)
    args = parser.parse_args()
    try:
        if args.action == "principal":
            print(
                principal_from_arm_token(
                    sys.stdin.read(), client_id=args.client_id, tenant_id=args.tenant_id
                )
            )
        else:
            verify_bootstrap_rbac(
                json.load(sys.stdin),
                expected_scope=args.expected_scope,
                expected_principal_id=args.expected_principal_id,
                expected_suffix=args.expected_suffix,
            )
            print("PASS: NONPROD application RG retains only the expected restricted bootstrap RBAC")
    except (ValueError, TypeError) as error:
        parser.exit(2, f"NONPROD bootstrap RBAC verification failed: {error}\n")


if __name__ == "__main__":
    main()
