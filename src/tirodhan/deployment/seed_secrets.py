"""Seed approved secret names from a private operator file, outside Terraform."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from tirodhan.deployment.azure_controls import vault_put

SECRET_NAMES = frozenset(
    {
        "google-maps-api-key",
        "kaleyra-api-key",
        "razorpay-key-secret",
        "razorpay-webhook-secret",
        "fcm-credentials-json",
        "auth-jwt-private-key-pem",
        "auth-jwt-public-key-pem",
        "phone-encryption-keys",
        "phone-lookup-hmac-key",
        "address-encryption-keys",
        "ghcr-pull-pat",
    }
)


def main() -> None:
    values = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not isinstance(values, dict) or set(values) != SECRET_NAMES:
        raise SystemExit("Supply exactly the documented required secret names")
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise SystemExit("Every secret must be a nonempty string")
    for name, value in values.items():
        vault_put(os.environ["KEY_VAULT_URL"], name, value)
    print("Required secret versions seeded; no values printed")


if __name__ == "__main__":
    main()
