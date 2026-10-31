"""Bootstrap RBAC checks prove preserved authority, not just a five-role count."""

from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from tirodhan.deployment.check_nonprod_bootstrap_rbac import (
    ADMIN_ROLE,
    BOOTSTRAP_ROLE_NAMES,
    BOOTSTRAP_ROLES,
    expected_admin_condition,
    principal_from_arm_token,
    verify_bootstrap_rbac,
)

SCOPE = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/tirodhan-np-tdnp01"
PRINCIPAL = "00000000-0000-0000-0000-000000000002"
CLIENT = "00000000-0000-0000-0000-000000000003"
TENANT = "00000000-0000-0000-0000-000000000004"
SUFFIX = "tdnp01"


def _assignments() -> list[dict[str, str | None]]:
    names = (*BOOTSTRAP_ROLE_NAMES, "Tirodhan NONPROD Deployment tdnp01")
    rows = [
        {
            "scope": SCOPE,
            "principalId": PRINCIPAL,
            "roleDefinitionName": name,
            "condition": None,
            "conditionVersion": None,
        }
        for name in names
    ]
    administrator = next(row for row in rows if row["roleDefinitionName"] == ADMIN_ROLE)
    administrator["condition"] = expected_admin_condition()
    administrator["conditionVersion"] = "2.0"
    return rows


def _verify(rows: list[dict[str, str | None]]) -> None:
    verify_bootstrap_rbac(
        rows,
        expected_scope=SCOPE,
        expected_principal_id=PRINCIPAL,
        expected_suffix=SUFFIX,
    )


def test_admin_condition_matches_authoritative_bootstrap_contract() -> None:
    script = Path("deploy/bootstrap/bootstrap.sh").read_text(encoding="utf-8")
    roles = re.search(r'^roles="([^"]+)"$', script, re.MULTILINE)
    condition = re.search(r'^condition="([^"]+)"$', script, re.MULTILINE)
    assert roles is not None and condition is not None
    assert roles.group(1) == "{" + ", ".join(BOOTSTRAP_ROLES) + "}"
    assert condition.group(1).replace("$roles", roles.group(1)) == expected_admin_condition()


def test_only_exact_original_bootstrap_grants_are_accepted() -> None:
    _verify(_assignments())


@pytest.mark.parametrize(
    ("name", "field", "value"),
    [
        ("Storage Blob Data Contributor", "principalId", CLIENT),
        ("Key Vault Secrets Officer", "scope", SCOPE + "/providers/Microsoft.KeyVault"),
        (ADMIN_ROLE, "condition", None),
        (ADMIN_ROLE, "condition", "true"),
        (ADMIN_ROLE, "conditionVersion", None),
        (ADMIN_ROLE, "conditionVersion", "1.0"),
    ],
)
def test_rejects_weak_or_unauthorized_bootstrap_grants(
    name: str, field: str, value: str | None
) -> None:
    rows = deepcopy(_assignments())
    next(row for row in rows if row["roleDefinitionName"] == name)[field] = value
    with pytest.raises(ValueError):
        _verify(rows)


def test_rejects_missing_or_duplicate_bootstrap_assignments() -> None:
    rows = _assignments()
    with pytest.raises(ValueError):
        _verify(rows[:-1])
    with pytest.raises(ValueError):
        _verify([*rows, rows[0]])
    rows[0]["roleDefinitionName"] = rows[1]["roleDefinitionName"]
    with pytest.raises(ValueError):
        _verify(rows)


def _token(claims: dict[str, str]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_authentication_claims_identify_deployment_object_not_client_id() -> None:
    token = _token({"appid": CLIENT, "tid": TENANT, "oid": PRINCIPAL})
    assert principal_from_arm_token(token, client_id=CLIENT, tenant_id=TENANT) == PRINCIPAL


@pytest.mark.parametrize(
    "claims",
    [
        {"appid": "another-client", "tid": TENANT, "oid": PRINCIPAL},
        {"appid": CLIENT, "tid": "another-tenant", "oid": PRINCIPAL},
        {"appid": CLIENT, "tid": TENANT},
    ],
)
def test_rejects_mismatched_or_incomplete_authentication(claims: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        principal_from_arm_token(_token(claims), client_id=CLIENT, tenant_id=TENANT)
