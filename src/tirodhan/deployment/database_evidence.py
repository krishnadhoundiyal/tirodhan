"""Pinned offline attester keys; never persist Azure credentials or secret SQL evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tirodhan.deployment.deployment_admission import ROOT, AdmissionDenied, Authorization


def signer(authorization: Authorization) -> tuple[str, Ed25519PrivateKey, int]:
    policy = json.loads((ROOT / "deploy/nonprod-release-policy.json").read_text())
    key_id = os.environ["TIRODHAN_DATABASE_EVIDENCE_KEY_ID"]
    issuer = policy["database_evidence_keys"][key_id]
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
    age = policy["maximum_evidence_age_seconds"]
    if type(age) is not int or age <= 0:
        raise AdmissionDenied("DATABASE_EVIDENCE_EXPIRY_INVALID")
    return key_id, private, age


def write_evidence(value: str) -> None:
    Path(os.environ["TIRODHAN_DATABASE_EVIDENCE_OUTPUT"]).write_text(value, encoding="utf-8")
