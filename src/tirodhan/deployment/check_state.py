"""Read terraform show -json on stdin; report paths only, never values."""

from __future__ import annotations

import json
import sys
from typing import Any

FORBIDDEN = {
    "primary_access_key",
    "secondary_access_key",
    "primary_connection_string",
    "secondary_connection_string",
    "primary_blob_connection_string",
    "secondary_blob_connection_string",
    "access_key",
    "sas_token",
    "account_key",
    "shared_key",
}


def populated_key_paths(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if value.get("key_vault_secret_id") and value.get("value") not in (None, ""):
            found.append(f"{path}.value")
        for key, child in value.items():
            # These are Terraform sensitivity/unknown annotations, not stored values.
            if key in {
                "sensitive_values",
                "sensitive_attributes",
                "before_sensitive",
                "after_sensitive",
                "after_unknown",
            }:
                continue
            child_path = f"{path}.{key}"
            if key.lower() in FORBIDDEN and child not in (None, "", [], {}):
                found.append(child_path)
            found.extend(populated_key_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(populated_key_paths(child, f"{path}[{index}]"))
    return found


def main() -> None:
    paths = populated_key_paths(json.load(sys.stdin))
    if paths:
        print("FAIL: populated credential attributes at " + ", ".join(paths))
        raise SystemExit(1)
    print("PASS: no populated storage-key/connection-string/SAS attributes")


if __name__ == "__main__":
    main()
