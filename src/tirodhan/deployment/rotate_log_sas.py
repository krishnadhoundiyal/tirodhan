"""Repeatable OIDC/CLI rotation. Each run creates an overlapping Key Vault version."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from azure.identity import AzureCliCredential
from azure.storage.blob import BlobServiceClient, ContainerSasPermissions, generate_container_sas

from tirodhan.deployment.azure_controls import vault_put


def main() -> None:
    account = os.environ["LOG_ACCOUNT_NAME"]
    vault = os.environ["KEY_VAULT_URL"]
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=6)
    start = now - timedelta(minutes=5)
    with (
        AzureCliCredential() as credential,
        BlobServiceClient(
            f"https://{account}.blob.core.windows.net",
            credential=credential,
        ) as client,
    ):
        key = client.get_user_delegation_key(start, expiry)
        sas = generate_container_sas(
            account_name=account,
            container_name="app-logs",
            user_delegation_key=key,
            permission=ContainerSasPermissions(write=True),
            start=start,
            expiry=expiry,
            protocol="https",
        )
        vault_put(vault, "fluent-bit-sas", sas, expiry=int(expiry.timestamp()))
    print("Log SAS rotated; verify Apps and new Job executions independently")


if __name__ == "__main__":
    main()
