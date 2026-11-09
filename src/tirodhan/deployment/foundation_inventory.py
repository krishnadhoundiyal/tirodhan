"""Reviewed stage-zero inventory; any Terraform source drift invalidates it."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from tirodhan.deployment.deployment_admission import ROOT, AdmissionDenied

BOOTSTRAP_FIREWALL_ADDRESS = "azurerm_postgresql_flexible_server_firewall_rule.bootstrap[0]"


def required_addresses(configuration: dict[str, Any]) -> set[str]:
    inventory = json.loads((ROOT / "deploy/nonprod-foundation-inventory.json").read_text())
    directory = ROOT / "infra/terraform/nonprod"
    actual = {
        path.name: hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for path in directory.glob("*.tf")
    }
    if inventory.get("schema_version") != 1 or inventory.get("sources") != actual:
        raise AdmissionDenied("FOUNDATION_INVENTORY_SOURCE_CHANGED")
    required: set[str] = set()
    for address, entry in inventory["resources"].items():
        if entry.get("when"):
            if entry["when"] != "postgres_bootstrap_ipv4":
                raise AdmissionDenied("UNIMPLEMENTED_FOUNDATION_CONDITION")
            if configuration.get("postgres_bootstrap_ipv4") is None:
                continue
        keys = entry["keys"]
        if keys is None:
            required.add(address)
        else:
            required.update(f"{address}[{json.dumps(key)}]" for key in keys)
    if not required:
        raise AdmissionDenied("FOUNDATION_INVENTORY_EMPTY")
    return required
