"""Seed approved secret names from a private operator file, outside Terraform."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from tirodhan.deployment.azure_controls import vault_put

LEGACY_SECRET_NAMES = frozenset(
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

# Separately seedable optional credentials preserve existing operator secret files.
CUSTOMER_DATA_SECRET_NAMES = frozenset(
    {"customer-cursor-signing-key", "customer-data-encryption-keys", "customer-data-hmac-key"}
)
CUSTOMER_WEB_PUSH_SECRET_NAMES = frozenset({"customer-vapid-private-key-pem"})
CUSTOMER_SECRET_NAMES = CUSTOMER_DATA_SECRET_NAMES | CUSTOMER_WEB_PUSH_SECRET_NAMES
SECRET_NAMES = LEGACY_SECRET_NAMES | CUSTOMER_SECRET_NAMES


def approved_secret_batch(names: set[str]) -> bool:
    return names in (
        LEGACY_SECRET_NAMES,
        CUSTOMER_DATA_SECRET_NAMES,
        CUSTOMER_WEB_PUSH_SECRET_NAMES,
        CUSTOMER_SECRET_NAMES,
        LEGACY_SECRET_NAMES | CUSTOMER_DATA_SECRET_NAMES,
        SECRET_NAMES,
    )


def main() -> None:
    values = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not isinstance(values, dict) or not approved_secret_batch(set(values)):
        raise SystemExit("Supply a complete approved legacy, customer-data, VAPID or combined secret batch")
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise SystemExit("Every secret must be a nonempty string")
    for name, value in values.items():
        vault_put(os.environ["KEY_VAULT_URL"], name, value)
    print("Approved secret versions seeded; no values printed")


if __name__ == "__main__":
    main()
