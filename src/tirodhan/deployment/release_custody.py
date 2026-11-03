"""Operator-only encrypted Ed25519 custody; never run on a deployment runner."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from tirodhan.deployment.deployment_admission import ROOT, AdmissionDenied, digest


def operator_only() -> None:
    if any(os.environ.get(name) for name in ("GITHUB_ACTIONS", "RUNNER_ENVIRONMENT", "CI")):
        raise AdmissionDenied("AUTHORIZER_REQUIRES_TRUSTED_OPERATOR_HOST")


def outside_repository(path: Path) -> Path:
    path = path.absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise AdmissionDenied("CUSTODY_SYMLINK_FORBIDDEN")
    resolved = path.resolve()
    if resolved == ROOT.resolve() or ROOT.resolve() in resolved.parents:
        raise AdmissionDenied("CUSTODY_MUST_BE_OUTSIDE_REPOSITORY")
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise AdmissionDenied("CUSTODY_MUST_BE_OUTSIDE_REPOSITORY")
    return resolved


def _windows_acl(path: Path, *, create: bool = False, protected: bool = True) -> None:
    # The only interpolated value is a single-quoted, escaped path, never key material.
    target = str(path).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User;"
        f"$p='{target}';"
    )
    if create:
        script += (
            "$acl=New-Object Security.AccessControl.DirectorySecurity;"
            "$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false);"
            "foreach($id in @($sid.Value,'S-1-5-18')) {"
            "$identity=New-Object Security.Principal.SecurityIdentifier($id);"
            "$rule=New-Object Security.AccessControl.FileSystemAccessRule("
            "$identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow');"
            "$acl.AddAccessRule($rule)};Set-Acl -LiteralPath $p -AclObject $acl;"
        )
    script += "$acl=Get-Acl -LiteralPath $p;"
    if protected:
        script += "if(!$acl.AreAccessRulesProtected){exit 1};"
    script += (
        "if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value){exit 1};"
        "foreach($r in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){"
        "if($r.AccessControlType -eq 'Allow' -and "
        "$r.IdentityReference.Value -notin @($sid.Value,'S-1-5-18')){exit 1}}"
    )
    try:
        result = subprocess.run(
            [
                shutil.which("pwsh.exe") or "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            timeout=15,
        )
        if result.returncode:
            raise ValueError
    except (OSError, ValueError, subprocess.SubprocessError):
        raise AdmissionDenied("CUSTODY_ACL_UNVERIFIED") from None


def private_directory(path: Path, *, create: bool = False) -> Path:
    operator_only()
    path = outside_repository(path)
    if create:
        path.mkdir(mode=0o700)  # Exclusive; existing directories are never re-permissioned.
        if os.name == "nt":
            _windows_acl(path, create=True)
    if not path.is_dir():
        raise AdmissionDenied("CUSTODY_DIRECTORY_UNAVAILABLE")
    if os.name == "nt":
        _windows_acl(path)
    else:
        info = path.stat()
        if (
            info.st_uid != int(getattr(os, "getuid", lambda: -1)())
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise AdmissionDenied("CUSTODY_DIRECTORY_NOT_PRIVATE")
    return path


def private_write(path: Path, content: bytes) -> None:
    private_directory(path.parent)
    outside_repository(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def private_read(path: Path) -> bytes:
    private_directory(path.parent)
    outside_repository(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise AdmissionDenied("CUSTODY_FILE_INVALID")
    if os.name != "nt" and (
        info.st_uid != int(getattr(os, "getuid", lambda: -1)())
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise AdmissionDenied("CUSTODY_FILE_NOT_PRIVATE")
    if os.name == "nt":
        _windows_acl(path, protected=False)
    if info.st_size > 64 * 1024 * 1024:
        raise AdmissionDenied("CUSTODY_FILE_TOO_LARGE")
    return path.read_bytes()


def public_pem(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def key_id(key: Ed25519PrivateKey) -> str:
    return "ed25519-" + digest(public_pem(key))


def load_private(path: Path, password: bytes) -> Ed25519PrivateKey:
    data = private_read(path)
    if not data.startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----"):
        raise AdmissionDenied("CUSTODY_REQUIRES_ENCRYPTED_PKCS8")
    try:
        key = serialization.load_pem_private_key(data, password=password)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError
        return key
    except (ValueError, TypeError):
        raise AdmissionDenied("CUSTODY_KEY_CANNOT_BE_UNLOCKED") from None


def generate_pair(directory: Path, password: bytes, verifier_principal: str) -> dict[str, Any]:
    from uuid import UUID

    UUID(verifier_principal)
    if not password:
        raise AdmissionDenied("CUSTODY_ENCRYPTION_PASSWORD_REQUIRED")
    if directory.exists():
        existing = json.loads(private_read(directory / "public-enrollment.json"))
        if not isinstance(existing, dict):
            raise AdmissionDenied("EXISTING_CUSTODY_METADATA_INVALID")
        release = load_private(directory / "release.encrypted.pem", password)
        database = load_private(directory / "database.encrypted.pem", password)
        if existing != {
            "authorization_keys": {key_id(release): public_pem(release)},
            "database_evidence_keys": {
                key_id(database): {
                    "public_key": public_pem(database),
                    "principal_id": verifier_principal,
                    "identity_kind": "database_verification",
                }
            },
        } or public_pem(release) == public_pem(database):
            raise AdmissionDenied("EXISTING_CUSTODY_IDENTITY_MISMATCH")
        return existing
    private_directory(directory, create=True)
    release, database = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    for name, key in (("release", release), ("database", database)):
        private_write(
            directory / f"{name}.encrypted.pem",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(password),
            ),
        )
        if public_pem(load_private(directory / f"{name}.encrypted.pem", password)) != public_pem(
            key
        ):
            raise AdmissionDenied("CUSTODY_KEY_READBACK_FAILED")
    proposal: dict[str, Any] = {
        "authorization_keys": {key_id(release): public_pem(release)},
        "database_evidence_keys": {
            key_id(database): {
                "public_key": public_pem(database),
                "principal_id": verifier_principal,
                "identity_kind": "database_verification",
            }
        },
    }
    private_write(directory / "public-enrollment.json", json.dumps(proposal, indent=2).encode())
    return proposal
