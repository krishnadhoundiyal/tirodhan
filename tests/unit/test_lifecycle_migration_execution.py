"""A preauthorized release can consume a subsequently created, verified migration."""

import copy
import time
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from lifecycle_fakes import SCOPE, MemoryObservations, auth

from tirodhan.deployment import foundation_reconcile, prerequisites, release
from tirodhan.deployment.deployment_admission import AdmissionDenied, validate_authorization
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.migration_evidence import API_VERSION

IMAGE = "ghcr.io/krishnadhoundiyal/tirodhan:" + "a" * 40
JOB = SCOPE + "/providers/Microsoft.App/jobs/tirodhan-np-test01-migrate"


@pytest.fixture
def migration(monkeypatch):
    monkeypatch.setenv("APPLICATION_IMAGE", IMAGE)
    monkeypatch.delenv("TIRODHAN_MIGRATION_EXECUTION", raising=False)
    io = MemoryObservations()
    io.current.resources["azurerm_container_app_job.migration[0]"] = JOB
    job = {
        "id": JOB,
        "properties": {
            "template": {
                "containers": [
                    {
                        "name": "application",
                        "image": IMAGE,
                        "command": ["python", "-m", "tirodhan.deployment.entrypoint"],
                        "args": [
                            "python",
                            "-m",
                            "tirodhan.deployment.job",
                            "--",
                            "alembic",
                            "upgrade",
                            "head",
                        ],
                    }
                ]
            }
        },
    }
    executions = {}
    reads = []

    def arm(resource_id, version):
        assert version == API_VERSION
        reads.append(resource_id)
        return copy.deepcopy(job if resource_id == JOB else executions.get(resource_id))

    io.arm = arm
    return io, job, executions, reads


def completed(job, name):
    return {"name": name, "properties": job["properties"] | {"status": "Succeeded"}}


@pytest.mark.parametrize("operation", ["runtime-plan", "runtime-apply"])
def test_signed_authorization_before_execution_exists_can_follow_workflow_output(
    migration, monkeypatch, tmp_path, operation
):
    io, job, executions, reads = migration
    authorization = auth(operation)
    now = int(time.time())
    claims = authorization.claims | {
        "iss": "tirodhan-nonprod-release",
        "aud": "tirodhan-nonprod-provisioning",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "sub": authorization.principal_id,
        "jti": "prequeued-release",
        "repository": "krishnadhoundiyal/tirodhan",
        "commit": authorization.commit,
        "run_id": authorization.run_id,
        "configuration_hash": authorization.configuration_hash,
        "contract_hash": authorization.contract_hash,
        "operations": [operation],
        "identity_kind": "deployment",
        "validation": {
            "ci_commit": authorization.commit,
            "contract_hash": authorization.contract_hash,
            "coderabbit_review": "reviewed-commit",
        },
    }
    key = Ed25519PrivateKey.generate()
    token = jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": "test"})
    assert executions == {} and "migration_execution" not in claims["bindings"][operation]
    authorization = validate_authorization(
        token,
        operation=operation,
        policy={
            "schema_version": 1,
            "contract_version": "nonprod-lifecycle-v1",
            "release_enabled": True,
            "authorization_keys": {
                "test": key.public_key()
                .public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                )
                .decode()
            },
        },
        config_hash=authorization.configuration_hash,
        code_hash=authorization.contract_hash,
        commit=authorization.commit,
        run_id=authorization.run_id,
    )
    checks = Checks(authorization, io.config, {}, io)
    assert not checks.check_migration()
    name = "migration-" + uuid4().hex
    executions[JOB + "/executions/" + name] = completed(job, name)
    io.runner.json = Mock(return_value={"name": name})
    monkeypatch.setattr(
        foundation_reconcile, "admitted_with_grant", lambda _: (checks, io, object())
    )
    monkeypatch.setattr(prerequisites, "revalidate_database", lambda _: None)
    output = tmp_path / "job-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    release.run_migration()
    key, value = output.read_text().strip().split("=", 1)
    assert key == "migration_execution" and value == name
    monkeypatch.setenv("TIRODHAN_MIGRATION_EXECUTION", value)
    assert Checks(authorization, io.config, {}, io).check_migration()
    assert reads[-1] == JOB + "/executions/" + name


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "job-id",
        "execution-id",
        "execution-name",
        "failed",
        "running",
        "image",
        "command",
        "args",
        "template",
        "duplicate-application",
    ],
)
def test_exact_execution_and_immutable_template_required(migration, monkeypatch, invalid):
    io, job, executions, _ = migration
    name = "migration-created-by-azure"
    value = completed(copy.deepcopy(job), name)
    executions[JOB + "/executions/" + name] = value
    application = value["properties"]["template"]["containers"][0]
    if invalid == "missing":
        executions.clear()
    elif invalid == "job-id":
        job["id"] += "-different"
    elif invalid == "execution-id":
        value["id"] = JOB + "-different/executions/" + name
    elif invalid == "execution-name":
        value["name"] = "another-execution"
    elif invalid in {"failed", "running"}:
        value["properties"]["status"] = invalid.title()
    elif invalid in {"image", "command", "args"}:
        application[invalid] = "unapproved"
    elif invalid == "template":
        del value["properties"]["template"]
    else:
        value["properties"]["template"]["containers"].append(copy.deepcopy(application))
    monkeypatch.setenv("TIRODHAN_MIGRATION_EXECUTION", name)
    checks = Checks(auth("runtime-plan"), io.config, {}, io)
    if invalid == "job-id":
        with pytest.raises(AdmissionDenied):
            checks.check_migration()
    else:
        assert not checks.check_migration()


@pytest.mark.parametrize("name", [None, "", "../other", "ok\nkey=value", "a" * 129])
def test_missing_or_invalid_output_cannot_fall_back_to_signed_execution(
    migration, monkeypatch, name
):
    io, job, executions, reads = migration
    executions[JOB + "/executions/old"] = completed(job, "old")
    if name is not None:
        monkeypatch.setenv("TIRODHAN_MIGRATION_EXECUTION", name)
    checks = Checks(auth("runtime-plan", migration_execution="old"), io.config, {}, io)
    assert not checks.check_migration()
    assert not any("/executions/" in resource for resource in reads)


@pytest.mark.parametrize("status", ["Failed", "Stopped", "Running", "Succeeded"])
def test_failed_uncertain_or_wrong_image_execution_emits_no_output(
    migration, monkeypatch, tmp_path, status
):
    io, job, executions, _ = migration
    name = "migration-created-by-azure"
    value = completed(copy.deepcopy(job), name)
    value["properties"]["status"] = status
    value["properties"]["template"]["containers"][0]["image"] = "old-image"
    executions[JOB + "/executions/" + name] = value
    io.runner.json = Mock(return_value={"name": name})
    checks = Checks(auth("migration-start"), io.config, {}, io)
    monkeypatch.setattr(
        foundation_reconcile, "admitted_with_grant", lambda _: (checks, io, object())
    )
    monkeypatch.setattr(prerequisites, "revalidate_database", lambda _: None)
    clock = iter([0, 0, 1201])
    monkeypatch.setattr(release.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(release.time, "sleep", lambda _: None)
    output = tmp_path / "job-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    with pytest.raises(release.ReleaseBlocked):
        release.run_migration()
    assert not output.exists()


def test_workflow_transports_execution_only_from_successful_migration_step():
    jobs = yaml.safe_load(Path(".github/workflows/nonprod-deploy.yml").read_text())["jobs"]
    apply, plan, runtime = (
        jobs[name] for name in ["migration-apply", "runtime-plan", "runtime-apply"]
    )
    assert (
        apply["outputs"]["migration_execution"]
        == "${{ steps.migration.outputs.migration_execution }}"
    )
    step = next(item for item in apply["steps"] if item.get("id") == "migration")
    assert step["run"] == "python -m tirodhan.deployment.release migrate"
    assert not step.get("continue-on-error") and "if" not in step
    assert plan["needs"] == "migration-apply"
    assert (
        plan["env"]["TIRODHAN_MIGRATION_EXECUTION"]
        == "${{ needs.migration-apply.outputs.migration_execution }}"
    )
    assert plan["outputs"]["migration_execution"] == plan["env"]["TIRODHAN_MIGRATION_EXECUTION"]
    assert runtime["needs"] == "runtime-plan"
    assert (
        runtime["env"]["TIRODHAN_MIGRATION_EXECUTION"]
        == "${{ needs.runtime-plan.outputs.migration_execution }}"
    )
