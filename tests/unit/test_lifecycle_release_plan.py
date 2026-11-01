"""Actual saved plans must carry the material and stage the prerequisites verified."""

import hashlib
import json
import subprocess

import pytest
from lifecycle_fakes import MemoryObservations, auth, write_plan

from tirodhan.deployment import prerequisites, release
from tirodhan.deployment.lifecycle_checks import Checks

APP = "ghcr.io/krishnadhoundiyal/tirodhan:" + "a" * 40
SIDECAR = "ghcr.io/krishnadhoundiyal/tirodhan-log-sidecar:" + "a" * 40
OLD_APP = APP.replace("a" * 40, "b" * 40)
OLD_SIDECAR = SIDECAR.replace("a" * 40, "b" * 40)


@pytest.mark.parametrize("mode", ["migration", "runtime"])
def test_only_migration_job_is_reconciled_before_schema_success(monkeypatch, mode):
    monkeypatch.setenv("APPLICATION_IMAGE", APP)
    monkeypatch.setenv("FLUENT_BIT_IMAGE", SIDECAR)
    monkeypatch.setattr(prerequisites, "require_provisioning", lambda _: object())
    monkeypatch.setattr(prerequisites, "revalidate_database", lambda _: None)
    monkeypatch.setattr(release, "assert_no_list_keys", lambda: None)
    monkeypatch.setattr(
        release,
        "output_values",
        lambda: {
            "deployment_stage": 4,
            "application_image": OLD_APP,
            "fluent_bit_image": OLD_SIDECAR,
        },
    )
    monkeypatch.setattr(release, "enabled_secret_names", lambda _: release.MIGRATION_SECRET_NAMES)
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({}), "")

    monkeypatch.setattr(release.subprocess, "run", run)
    release.plan(mode, "reviewed.tfplan")
    targets = [value for value in commands[0] if value.startswith("-target=")]
    assert targets == (
        ["-target=azurerm_container_app_job.migration[0]"] if mode == "migration" else []
    )


@pytest.mark.parametrize("operation", ["migration-apply", "runtime-apply"])
@pytest.mark.parametrize(
    "changed", [None, "migration_image", "application_image", "fluent_bit_image"]
)
def test_saved_plan_binds_images_to_release_and_preserved_runtime(
    monkeypatch, tmp_path, operation, changed
):
    io, checks = plan_case(monkeypatch, tmp_path, operation)
    if changed:
        io.runner.plan["variables"][changed]["value"] = "ghcr.io/unapproved/image:" + "c" * 40
    assert checks.check_saved_plan() is (changed is None)


def plan_case(monkeypatch, tmp_path, operation):
    monkeypatch.setenv("APPLICATION_IMAGE", APP)
    monkeypatch.setenv("FLUENT_BIT_IMAGE", SIDECAR)
    io = MemoryObservations()
    io.current.document["outputs"] = {
        "deployment_stage": {"value": 4},
        "application_image": {"value": OLD_APP},
        "fluent_bit_image": {"value": OLD_SIDECAR},
    }
    runtime = operation == "runtime-apply"
    io.runner.plan["variables"].update(
        {
            key: {"value": value}
            for key, value in {
                "deployment_stage": 4,
                "migration_image": APP,
                "application_image": APP if runtime else OLD_APP,
                "fluent_bit_image": SIDECAR if runtime else OLD_SIDECAR,
                "enabled_runtime_secret_names": sorted(release.RUNTIME_SECRET_NAMES),
            }.items()
        }
    )
    path = tmp_path / "reviewed.tfplan"
    write_plan(path, io.current)
    authorization = auth(
        operation,
        plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        state_fingerprint=io.current.fingerprint,
    )
    return io, Checks(authorization, io.config, {"plan": path}, io)


@pytest.mark.parametrize("kind", ["azurerm_container_app", "azurerm_container_app_job"])
def test_migration_apply_rejects_runtime_mutation_even_with_approved_hash(
    monkeypatch, tmp_path, kind
):
    io, checks = plan_case(monkeypatch, tmp_path, "migration-apply")
    io.runner.plan["resource_changes"] = [
        {
            "address": kind + ".runtime[0]",
            "mode": "managed",
            "type": kind,
            "change": {"actions": ["update"]},
        }
    ]
    assert not checks.check_saved_plan()


@pytest.mark.parametrize("names", [[], ["foreign-secret"], None, ["phone-encryption-keys"]])
def test_runtime_apply_rejects_missing_or_unapproved_material_bindings(
    monkeypatch, tmp_path, names
):
    io, checks = plan_case(monkeypatch, tmp_path, "runtime-apply")
    io.runner.plan["variables"]["enabled_runtime_secret_names"]["value"] = names
    assert not checks.check_saved_plan()
