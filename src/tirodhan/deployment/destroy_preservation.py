"""Exact retained-vault inventory handling; does not authenticate or mutate resources."""

from __future__ import annotations

import os
import sys
from typing import Any

from tirodhan.deployment.deployment_admission import AdmissionDenied, configuration
from tirodhan.deployment.lifecycle_evidence import Observations


def verify_retained_vault(io: Observations) -> None:
    value = io.arm(io.vault_id, "2023-07-01")
    props: dict[str, Any] = (value or {}).get("properties", {})
    if value is None or (
        str(value.get("id", "")).casefold() != io.vault_id.casefold()
        or str(value.get("location", "")).casefold() != io.tf["location"].casefold()
        or str(props.get("tenantId", "")).casefold() != io.tf["tenant_id"].casefold()
        or props.get("enablePurgeProtection") is not True
        or props.get("enableRbacAuthorization") is not True
        or props.get("provisioningState") != "Succeeded"
    ):
        raise AdmissionDenied("RETAINED_BOOTSTRAP_VAULT_NOT_VERIFIED")


def disposable_inventory(lines: list[str], io: Observations) -> list[str]:
    remaining = []
    for item in lines:
        if item.casefold() == io.vault_id.casefold():
            verify_retained_vault(io)
        elif item:
            remaining.append(item)  # Every other resource, including foreign vaults, remains fatal.
    return remaining


def retention_required(io: Observations) -> bool:
    if io.arm(io.vault_id, "2023-07-01") is not None:
        verify_retained_vault(io)
        return True
    state = io.backend()
    if state.kind != "INITIALIZED" or state.resources or io.inventory():
        raise AdmissionDenied("MISSING_PERSISTENT_VAULT_OUTSIDE_VERIFIED_LEGACY_CLEAN_BACKEND")
    return False


def main() -> None:
    record = "--record-required" in sys.argv[1:]
    enforce = "--verify-expectation" in sys.argv[1:]
    lines = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    if not lines and not record and not enforce:
        return
    try:
        io = Observations(configuration())
        if record:
            print("true" if retention_required(io) else "false")
            return
        if enforce:
            expectation = os.environ.get("REQUIRE_PERSISTENT_VAULT")
            if expectation == "true":
                verify_retained_vault(io)
            elif expectation == "false":
                retention_required(
                    io
                )  # Recheck actual legacy-clean facts or an active exact vault.
            else:
                raise AdmissionDenied("MISSING_PLAN_JOB_RETENTION_EXPECTATION")
        print("\n".join(disposable_inventory(lines, io)))
    except (AdmissionDenied, OSError, ValueError, KeyError):
        if record:
            raise SystemExit("BOOTSTRAP_PRESERVATION_NOT_VERIFIED") from None
        # Unknown observations remain visible and block the calling teardown step.
        print("\n".join(lines) or "RETAINED_BOOTSTRAP_VAULT_NOT_VERIFIED")


if __name__ == "__main__":
    main()
