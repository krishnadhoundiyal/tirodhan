"""Pinned offline attester keys; never persist Azure credentials or secret SQL evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from tirodhan.deployment.deployment_admission import ROOT, AdmissionDenied, Authorization


def verification_key_is_independent(policy: dict[str, Any], key_id: str) -> bool:
    def raw(value: str) -> bytes:
        key = serialization.load_pem_public_key(value.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise AdmissionDenied("DATABASE_EVIDENCE_PUBLIC_KEY_INVALID")
        return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    public = raw(policy["database_evidence_keys"][key_id]["public_key"])
    others = list(policy["authorization_keys"].values()) + [
        row["public_key"]
        for name, row in policy["database_evidence_keys"].items()
        if name != key_id
    ]
    return all(raw(value) != public for value in others)


def signer(authorization: Authorization) -> tuple[str, Ed25519PrivateKey, int]:
    policy = json.loads((ROOT / "deploy/nonprod-release-policy.json").read_text())
    key_id = os.environ["TIRODHAN_DATABASE_EVIDENCE_KEY_ID"]
    issuer = policy["database_evidence_keys"][key_id]
    if (
        authorization.identity_kind == "database_verification"
        and issuer.get("identity_kind") != "database_verification"
    ):
        raise AdmissionDenied("DATABASE_VERIFICATION_KEY_NOT_INDEPENDENTLY_PINNED")
    private = serialization.load_pem_private_key(
        Path(os.environ["TIRODHAN_DATABASE_EVIDENCE_PRIVATE_KEY_FILE"]).read_bytes(), password=None
    )
    if not isinstance(private, Ed25519PrivateKey):
        raise AdmissionDenied("DATABASE_EVIDENCE_SIGNER_INVALID")
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    if issuer["principal_id"] != authorization.principal_id or issuer["public_key"] != public:
        raise AdmissionDenied("DATABASE_EVIDENCE_SIGNER_NOT_AUTHORIZED")
    if (
        authorization.identity_kind == "database_verification"
        and not verification_key_is_independent(policy, key_id)
    ):
        raise AdmissionDenied("DATABASE_VERIFICATION_SIGNING_KEY_MUST_BE_SEPARATE")
    age = policy["maximum_evidence_age_seconds"]
    if type(age) is not int or age <= 0:
        raise AdmissionDenied("DATABASE_EVIDENCE_EXPIRY_INVALID")
    return key_id, private, age


def write_evidence(value: str) -> None:
    Path(os.environ["TIRODHAN_DATABASE_EVIDENCE_OUTPUT"]).write_text(value, encoding="utf-8")
