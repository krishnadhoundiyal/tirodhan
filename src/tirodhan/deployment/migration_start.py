"""One conditional durable claim per release; unknown starts are reconciled, never repeated."""

from __future__ import annotations

import json
from typing import Any

from azure.core.exceptions import AzureError, ResourceExistsError
from azure.identity import AzureCliCredential
from azure.storage.blob import BlobClient

from tirodhan.deployment.deployment_admission import AdmissionDenied, Authorization, digest
from tirodhan.deployment.lifecycle_evidence import ObservationError, Observations
from tirodhan.deployment.migration_evidence import API_VERSION, execution_name

MARKER = "TIRODHAN_MIGRATION_OPERATION_KEY"


def expected_job_id(config: dict[str, Any]) -> str:
    tf = config["terraform"]
    return (
        f"/subscriptions/{tf['subscription_id']}/resourceGroups/{tf['resource_group_name']}"
        f"/providers/Microsoft.App/jobs/tirodhan-np-{tf['suffix']}-migrate"
    )


def operation_key(authorization: Authorization, job_id: str, image: str) -> str:
    # Attempts, auth-token renewal and current TF serial must not change the business key.
    return digest(
        {
            "version": 1,
            "release": authorization.run_id,
            "commit": authorization.commit,
            "configuration": authorization.configuration_hash,
            "source": authorization.contract_hash,
            "job": job_id.casefold(),
            "image": image,
        }
    )


class ClaimStore:
    """Uses D's existing account-scoped blob role, never state-RG Reader or account keys."""

    def __init__(self, io: Observations) -> None:
        self.account = io.config["state_account"]
        self.credential = AzureCliCredential(tenant_id=io.tf["tenant_id"], process_timeout=30)

    def close(self) -> None:
        self.credential.close()

    def acquire(self, key: str, owner: str) -> bool:
        value = {"schema_version": 1, "operation_key": key, "owner": owner}
        payload = json.dumps(value, sort_keys=True).encode()
        with BlobClient(
            account_url=f"https://{self.account}.blob.core.windows.net",
            container_name="tfstate",
            blob_name=f"tirodhan/migration-starts/{key}.json",
            credential=self.credential,
            retry_total=0,
            connection_timeout=10,
            read_timeout=30,
        ) as blob:
            try:
                # Block-blob overwrite=False atomically uses If-None-Match: *.
                blob.upload_blob(payload, overwrite=False)
                return True
            except ResourceExistsError:
                pass
            except AzureError:
                # Upload may have committed before its response was lost. Only this
                # fresh process's unguessable owner can proceed after confirmed read-back.
                pass
            try:
                data = blob.download_blob(offset=0, length=4097, max_concurrency=1).readall()
                if len(data) > 4096:
                    raise ValueError
                stored = json.loads(data)
                if (
                    not isinstance(stored, dict)
                    or stored.get("schema_version") != 1
                    or stored.get("operation_key") != key
                    or not isinstance(stored.get("owner"), str)
                    or len(stored["owner"]) != 32
                ):
                    raise ValueError
                return bool(stored["owner"] == owner)
            except (AzureError, ValueError, TypeError) as exc:
                raise ObservationError("MIGRATION_START_CLAIM_UNVERIFIED") from exc


def belongs_to_release(value: dict[str, Any], key: str) -> bool:
    applications = [
        item
        for item in value.get("properties", {}).get("template", {}).get("containers", [])
        if item.get("name") == "application"
    ]
    if len(applications) != 1:
        return False
    markers = [item for item in applications[0].get("env", []) if item.get("name") == MARKER]
    return (
        len(markers) == 1
        and markers[0].get("value") == key
        and markers[0].get("secretRef") in (None, "")
    )


def reconcile(io: Observations, job_id: str, key: str) -> str | None:
    value = io.arm(job_id + "/executions", API_VERSION)
    if value is None or not isinstance(value.get("value"), list):
        raise AdmissionDenied("MIGRATION_START_RECONCILIATION_UNAVAILABLE")
    # Azure retains bounded execution history. Not finding a receipt is UNKNOWN,
    # including truncated history, and never permission to issue a replacement start.
    matches = [item for item in value["value"] if belongs_to_release(item, key)]
    if len(matches) > 1:
        raise AdmissionDenied("MIGRATION_START_MULTIPLE_EXECUTIONS")
    if not matches:
        return None
    name = matches[0].get("name")
    if not execution_name(name):
        raise AdmissionDenied("MIGRATION_START_EXECUTION_UNVERIFIED")
    return name
