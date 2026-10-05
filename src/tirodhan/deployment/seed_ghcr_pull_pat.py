"""Seed the NONPROD GHCR pull PAT into Key Vault without printing its value."""

from __future__ import annotations

import os

from tirodhan.deployment.azure_controls import vault_put

SECRET_NAME = "ghcr-pull-pat"


def main() -> None:
    vault_url = os.environ["KEY_VAULT_URL"]
    value = os.environ["GHCR_PULL_PAT"]
    if not value.strip():
        raise SystemExit("GHCR_PULL_PAT must be nonempty")
    vault_put(vault_url, SECRET_NAME, value)
    print("GHCR pull credential seeded")


if __name__ == "__main__":
    main()
