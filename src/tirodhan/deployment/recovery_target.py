"""Exact deleted-vault generation bindings, shared by capture and live admission."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from tirodhan.deployment.deployment_admission import AdmissionDenied, maintenance_target

if TYPE_CHECKING:
    from tirodhan.deployment.lifecycle_checks import Checks


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AdmissionDenied("RECOVERY_TIMESTAMP_MUST_HAVE_TIMEZONE")
    return parsed.astimezone(timezone.utc)


def deleted_target(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    target = maintenance_target(config)
    name = target["vault_id"].rsplit("/", 1)[-1]
    matches = [r for r in rows if str(r.get("name", "")).casefold() == name.casefold()]
    if len(matches) != 1:
        raise AdmissionDenied("RECOVERY_DELETED_TARGET_AMBIGUOUS_OR_ABSENT")
    row = matches[0]
    properties = row.get("properties", {})
    location = properties.get("location") or row.get("location")
    identifier = (
        f"/subscriptions/{target['subscription_id']}/providers/Microsoft.KeyVault/"
        f"locations/{target['location']}/deletedVaults/{name}"
    )
    deleted = timestamp(properties["deletionDate"])
    purge = timestamp(properties["scheduledPurgeDate"])
    retention = (purge - deleted).total_seconds() / 86400
    if (
        str(row.get("id", "")).casefold() != identifier.casefold()
        or str(properties.get("vaultId", "")).casefold() != target["vault_id"].casefold()
        or str(location).casefold() != target["location"].casefold()
        or properties.get("purgeProtectionEnabled") is not True
        or not 7 <= retention <= 90
        or not retention.is_integer()
        or deleted > datetime.now(timezone.utc)
    ):
        raise AdmissionDenied("RECOVERY_DELETED_TARGET_INVALID")
    return {
        "id": identifier,
        "vault_id": target["vault_id"],
        "location": target["location"],
        "deletion_date": deleted.isoformat(),
        "scheduled_purge_date": purge.isoformat(),
        "purge_protection": True,
        "retention_days": int(retention),
    }


def validate_deleted_context(value: dict[str, Any], config: dict[str, Any]) -> None:
    reconstructed = deleted_target(
        [
            {
                "id": value["id"],
                "name": value["vault_id"].rsplit("/", 1)[-1],
                "properties": {
                    "vaultId": value["vault_id"],
                    "location": value["location"],
                    "deletionDate": value["deletion_date"],
                    "scheduledPurgeDate": value["scheduled_purge_date"],
                    "purgeProtectionEnabled": value["purge_protection"],
                },
            }
        ],
        config,
    )
    if reconstructed != value:
        raise AdmissionDenied("RECOVERY_DELETED_CONTEXT_INVALID")


def active_target_state(checks: Checks) -> str | None:
    """An in-progress exact target may be observed, never recovered again."""
    value = checks.io.arm(checks.io.vault_id, "2023-07-01")
    if value is None:
        return None
    properties = value.get("properties", {})
    deleted = checks.binding().get("deleted_vault", {})
    validate_deleted_context(deleted, checks.config)
    valid = (
        str(value.get("id", "")).casefold() == checks.io.vault_id.casefold()
        and str(value.get("location", "")).casefold() == checks.tf["location"].casefold()
        and str(properties.get("tenantId", "")).casefold() == checks.tf["tenant_id"].casefold()
        and properties.get("enablePurgeProtection") is True
        and properties.get("enableRbacAuthorization") is True
        and properties.get("enableSoftDelete") is True
        and properties.get("softDeleteRetentionInDays") == deleted["retention_days"]
        and properties.get("provisioningState")
        in {"Succeeded", "Creating", "Updating", "Recovering"}
    )
    if not valid:
        raise AdmissionDenied("RECOVERY_ACTIVE_TARGET_UNVERIFIED")
    return str(properties["provisioningState"])


def valid_active_target(checks: Checks) -> bool:
    return active_target_state(checks) is not None
