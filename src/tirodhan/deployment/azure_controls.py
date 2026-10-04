"""Operator/CI helpers: Entra-only access, no secret diagnostics."""

from __future__ import annotations

import fnmatch
import os
from typing import Any

import httpx
from azure.identity import AzureCliCredential

LIST_KEYS = "Microsoft.Storage/storageAccounts/listKeys/action"


def permission_allows_list_keys(permissions: list[dict[str, Any]]) -> bool:
    action = LIST_KEYS.casefold()
    return any(
        any(fnmatch.fnmatchcase(action, pattern.casefold()) for pattern in grant.get("actions", []))
        and not any(
            fnmatch.fnmatchcase(action, pattern.casefold())
            for pattern in grant.get("notActions", [])
        )
        for grant in permissions
    )


def vault_put(vault_url: str, name: str, value: str, *, expiry: int | None = None) -> None:
    with AzureCliCredential() as credential, httpx.Client(timeout=30) as client:
        token = credential.get_token("https://vault.azure.net/.default").token
        payload: dict[str, Any] = {"value": value}
        if expiry is not None:
            payload["attributes"] = {"exp": expiry}
        response = client.put(
            f"{vault_url.rstrip('/')}/secrets/{name}?api-version=7.4",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        if not response.is_success:
            raise RuntimeError(
                "Key Vault secret write failed; inspect Azure operation status safely"
            )


def assert_no_list_keys() -> None:
    scope = os.environ["DEPLOYMENT_SCOPE"]
    with AzureCliCredential() as credential, httpx.Client(timeout=30) as client:
        token = credential.get_token("https://management.azure.com/.default").token
        response = client.get(
            f"https://management.azure.com{scope}/providers/Microsoft.Authorization/permissions"
            "?api-version=2022-04-01",
            headers={"Authorization": f"Bearer {token}"},
        )
        if not response.is_success:
            raise RuntimeError("Unable to verify effective deployment permissions")
        permissions = response.json().get("value", [])
        if permission_allows_list_keys(permissions):
            raise RuntimeError("Deployment identity can list storage keys; remove inherited grants")
    print("PASS: effective deployment scope excludes storage listKeys")


if __name__ == "__main__":
    assert_no_list_keys()
