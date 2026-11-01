"""Protected OIDC verifier: SQL observations and attestation only; never enrollment/grants."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from tirodhan.deployment.bootstrap_database import write_verified_evidence
from tirodhan.deployment.database_verification_request import Github, validate_request
from tirodhan.deployment.deployment_admission import (
    AdmissionDenied,
    Authorization,
    configuration,
    require_release_authorization,
)
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.prerequisites import Controller, emit_grant


def authorize() -> tuple[Authorization, dict[str, str]]:
    authorization = require_release_authorization("database-verify")
    request = validate_request(
        authorization, json.loads(os.environ["DATABASE_VERIFICATION_REQUEST"]), Github()
    )
    if request["request_id"] != os.environ.get("DATABASE_VERIFICATION_REQUEST_ID"):
        raise AdmissionDenied("DATABASE_VERIFICATION_REQUEST_ID_MISMATCH")
    return authorization, request


def verify() -> None:
    key = os.environ.pop("DATABASE_EVIDENCE_PRIVATE_KEY_PEM")
    authorization, request = authorize()  # Recheck the consumer after any approval delay.
    config = configuration()
    root = Path(os.environ["RUNNER_TEMP"])
    output = root / "database-evidence.jwt"
    output.unlink(missing_ok=True)  # A failed rerun cannot upload a prior successful report.
    with tempfile.TemporaryDirectory(prefix="tirodhan-database-verifier-", dir=root) as temporary:
        key_path = Path(temporary) / "attester.pem"
        descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(key)
        del key
        previous = os.environ.get("TIRODHAN_DATABASE_EVIDENCE_PRIVATE_KEY_FILE")
        os.environ["TIRODHAN_DATABASE_EVIDENCE_PRIVATE_KEY_FILE"] = str(key_path)
        os.environ["TIRODHAN_DATABASE_EVIDENCE_OUTPUT"] = str(output)
        try:
            checks = Checks(authorization, config, {"request": request})
            emit_grant(Controller(authorization, checks).evaluate("database-verify"))
            target = checks.database_target()
            asyncio.run(
                write_verified_evidence(
                    target,
                    authorization,
                    database_user=f"tirodhan-np-{config['terraform']['suffix']}-database-verifier",
                    consumer_context=request,
                )
            )
        finally:
            if previous is None:
                os.environ.pop("TIRODHAN_DATABASE_EVIDENCE_PRIVATE_KEY_FILE", None)
            else:
                os.environ["TIRODHAN_DATABASE_EVIDENCE_PRIVATE_KEY_FILE"] = previous
    print("DATABASE_VERIFICATION_COMPLETED: signed facts for exact active consumer")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["authorize", "verify"])
    args = parser.parse_args()
    try:
        if args.operation == "authorize":
            authorize()
        else:
            verify()
    except Exception:
        raise SystemExit("DATABASE_VERIFICATION_BLOCKED: no readiness evidence issued") from None


if __name__ == "__main__":
    main()
