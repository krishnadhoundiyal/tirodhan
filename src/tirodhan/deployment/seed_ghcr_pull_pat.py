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
    from tirodhan.deployment.prerequisites import require_provisioning

    require_provisioning("seed-secrets", batch={SECRET_NAME: value})
    vault_put(vault_url, SECRET_NAME, value)
    print("GHCR pull credential seeded")


if __name__ == "__main__":
    from tirodhan.deployment.deployment_admission import (
        AdmissionDenied,
        require_release_authorization,
    )

    try:
        require_release_authorization("seed-secrets")
        main()
    except AdmissionDenied as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("PROVISIONING_UNVERIFIED: inspect operation status safely") from None
