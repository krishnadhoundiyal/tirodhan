"""Create the gateway invocation secret through the approved NONPROD workflow."""

from __future__ import annotations

import os
from secrets import token_urlsafe

from tirodhan.deployment.azure_controls import vault_put
from tirodhan.deployment.deployment_admission import AdmissionDenied, require_release_authorization
from tirodhan.deployment.foundation_reconcile import admitted_with_grant
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.lifecycle_evidence import Observations

SECRET_NAME = "gateway-secret"


def gateway_secret_exists(io: Observations) -> bool:
    rows = io.runner.json(
        "az",
        "keyvault",
        "secret",
        "list",
        "--vault-name",
        io.vault_id.rsplit("/", 1)[-1],
        "--output",
        "json",
    )
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in rows
    ):
        raise AdmissionDenied("GATEWAY_SECRET_METADATA_UNAVAILABLE")

    matching = [row for row in rows if row["name"].casefold() == SECRET_NAME]
    if not matching:
        return False
    if len(matching) != 1 or not Checks.secret_metadata_usable(matching[0].get("attributes")):
        raise AdmissionDenied("EXISTING_GATEWAY_SECRET_REQUIRES_REVIEW")
    return True


def main() -> None:
    require_release_authorization("seed-secrets")
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("TIRODHAN_NONPROD_SEED_TARGET") != SECRET_NAME
    ):
        raise AdmissionDenied("GATEWAY_SECRET_WORKFLOW_REQUIRED")
    vault_url = os.environ["KEY_VAULT_URL"]
    value = token_urlsafe(48)
    _, io, _ = admitted_with_grant("seed-secrets", batch={SECRET_NAME: value})
    if gateway_secret_exists(io):
        print("Existing usable gateway secret preserved")
        return
    vault_put(vault_url, SECRET_NAME, value)
    if not gateway_secret_exists(io):
        raise AdmissionDenied("GATEWAY_SECRET_POSTCONDITION_UNVERIFIED")
    print("Gateway secret created and metadata verified")


if __name__ == "__main__":
    try:
        main()
    except AdmissionDenied as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("PROVISIONING_UNVERIFIED: inspect operation status safely") from None
