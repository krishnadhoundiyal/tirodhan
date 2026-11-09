"""Actual saved plans must carry the material and stage the prerequisites verified."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from lifecycle_fakes import CONFIG, SCOPE, MemoryObservations, auth, state, write_plan

from tirodhan.deployment import deployment_admission, prerequisites, release
from tirodhan.deployment.foundation_inventory import BOOTSTRAP_FIREWALL_ADDRESS
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.migration_start import expected_job_id, operation_key

APP = "ghcr.io/krishnadhoundiyal/tirodhan:" + "a" * 40
SIDECAR = "ghcr.io/krishnadhoundiyal/tirodhan-log-sidecar:" + "a" * 40
OLD_APP = APP.replace("a" * 40, "b" * 40)
OLD_SIDECAR = SIDECAR.replace("a" * 40, "b" * 40)


@pytest.mark.parametrize("mode", ["migration", "runtime"])
def test_only_migration_job_is_reconciled_before_schema_success(monkeypatch, mode):
    monkeypatch.setenv("APPLICATION_IMAGE", APP)
    monkeypatch.setenv("FLUENT_BIT_IMAGE", SIDECAR)
    monkeypatch.setattr(
        prerequisites, "require_provisioning", lambda op: SimpleNamespace(authorization=auth(op))
    )
    monkeypatch.setattr(deployment_admission, "configuration", lambda: CONFIG)
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
    environments = []

    def run(args, **kwargs):
        commands.append(args)
        environments.append(kwargs.get("env"))
        return subprocess.CompletedProcess(args, 0, json.dumps({}), "")

    monkeypatch.setattr(release.subprocess, "run", run)
    release.plan(mode, "reviewed.tfplan")
    assert environments[0]["TF_VAR_migration_operation_key"] == operation_key(
        auth(f"{mode}-plan"), expected_job_id(CONFIG), APP
    )
    targets = [value for value in commands[0] if value.startswith("-target=")]
    assert targets == (
        ["-target=azurerm_container_app_job.migration[0]"] if mode == "migration" else []
    )


@pytest.mark.parametrize("mode", ["migration", "runtime"])
def test_terraform_plan_timeout_blocks_without_inspecting_unfinished_plan(monkeypatch, mode):
    monkeypatch.setenv("APPLICATION_IMAGE", APP)
    monkeypatch.setenv("FLUENT_BIT_IMAGE", SIDECAR)
    monkeypatch.setattr(
        prerequisites, "require_provisioning", lambda op: SimpleNamespace(authorization=auth(op))
    )
    monkeypatch.setattr(deployment_admission, "configuration", lambda: CONFIG)
    monkeypatch.setattr(prerequisites, "revalidate_database", lambda _: None)
    monkeypatch.setattr(release, "assert_no_list_keys", lambda: None)
    monkeypatch.setattr(release, "output_values", lambda: {"deployment_stage": 1})
    monkeypatch.setattr(release, "enabled_secret_names", lambda _: release.MIGRATION_SECRET_NAMES)
    invoked = []

    def hung(args, **kwargs):
        invoked.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(release.subprocess, "run", hung)
    with pytest.raises(release.ReleaseBlocked, match="Terraform planning timed out"):
        release.plan(mode, "reviewed.tfplan")
    assert len(invoked) == 1
    assert invoked[0][0][:2] == ["terraform", "plan"]
    assert invoked[0][1]["timeout"] == 1800


@pytest.mark.parametrize("operation", ["migration-apply", "runtime-apply"])
@pytest.mark.parametrize(
    "changed",
    [None, "migration_image", "application_image", "fluent_bit_image", "migration_operation_key"],
)
def test_saved_plan_binds_images_to_release_and_preserved_runtime(
    monkeypatch, tmp_path, operation, changed
):
    io, checks = plan_case(monkeypatch, tmp_path, operation)
    if changed:
        io.runner.plan["variables"][changed]["value"] = "ghcr.io/unapproved/image:" + "c" * 40
    assert checks.check_saved_plan() is (changed is None)


def plan_case(monkeypatch, tmp_path, operation, *, current_stage=4):
    """Bind a synthetic saved plan to its matching state, operation and release images."""
    monkeypatch.setenv("APPLICATION_IMAGE", APP)
    monkeypatch.setenv("FLUENT_BIT_IMAGE", SIDECAR)
    io = MemoryObservations()
    io.current.document["outputs"] = {
        "deployment_stage": {"value": current_stage},
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
                "migration_operation_key": operation_key(
                    auth(operation), expected_job_id(io.config), APP
                ),
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


@pytest.mark.parametrize(
    ("operation", "current_stage", "expected_stage"),
    [
        ("foundation-apply", 0, 0),
        ("migration-apply", 0, 1),
        ("migration-apply", 2, 2),
        ("migration-apply", 4, 4),
        ("runtime-apply", 1, 4),
    ],
)
@pytest.mark.parametrize("representation", ["number", "environment", "wrong", "bool", "float"])
def test_saved_plan_requires_exact_operation_stage(
    monkeypatch, tmp_path, operation, current_stage, expected_stage, representation
):
    """Admit only the operation's exact stage without coercing other scalar types."""
    io, checks = plan_case(monkeypatch, tmp_path, operation, current_stage=current_stage)
    values = {
        "number": expected_stage,
        "environment": str(expected_stage),
        "wrong": str((expected_stage + 1) % 5),
        "bool": bool(expected_stage),
        "float": float(expected_stage),
    }
    io.runner.plan["variables"]["deployment_stage"]["value"] = values[representation]
    assert checks.check_saved_plan() is (representation in {"number", "environment"})


@pytest.mark.parametrize("stage", [None, "", "00", "0.0", "+0", " 0", "0 ", "0e0", [], {}])
def test_saved_plan_rejects_noncanonical_foundation_stage(monkeypatch, tmp_path, stage):
    """Reject missing, malformed and alternative numeric spellings before apply."""
    io, checks = plan_case(monkeypatch, tmp_path, "foundation-apply", current_stage=0)
    io.runner.plan["variables"]["deployment_stage"]["value"] = stage
    assert not checks.check_saved_plan()


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "different-rule",
        "different-server",
        "replacement",
        "extra-update",
        "move",
        "enabled-input",
        "later-stage",
        "migration",
        "stale-state",
    ],
)
def test_saved_plan_only_allows_exact_stage_zero_bootstrap_firewall_removal(
    monkeypatch, tmp_path, invalid
):
    operation = "migration-apply" if invalid == "migration" else "foundation-apply"
    io, checks = plan_case(monkeypatch, tmp_path, operation, current_stage=0)
    server = f"{SCOPE}/providers/Microsoft.DBforPostgreSQL/flexibleServers/test-pg"
    rule_id = f"{server}/firewallRules/temporary-bootstrap"
    outputs = io.current.document["outputs"]
    io.current = state(
        {
            "azurerm_postgresql_flexible_server.database": server,
            BOOTSTRAP_FIREWALL_ADDRESS: rule_id,
        }
    )
    outputs["deployment_stage"]["value"] = 1 if invalid == "later-stage" else 0
    io.current.document["outputs"] = outputs
    path = checks.parameters["plan"]
    write_plan(path, io.current)
    checks.auth = auth(
        operation,
        plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        state_fingerprint=io.current.fingerprint,
    )
    io.runner.plan["variables"]["deployment_stage"]["value"] = 1 if invalid == "migration" else 0
    io.runner.plan["variables"]["postgres_bootstrap_ipv4"] = {
        "value": "192.0.2.1" if invalid == "enabled-input" else None,
    }
    change = {"actions": ["delete"], "before": {"id": rule_id}, "after": None}
    row = {
        "address": BOOTSTRAP_FIREWALL_ADDRESS,
        "type": "azurerm_postgresql_flexible_server_firewall_rule",
        "change": change,
    }
    io.runner.plan["resource_changes"] = [row]
    if invalid == "different-rule":
        change["before"]["id"] = f"{server}/firewallRules/nonprod-azure-origin"
    elif invalid == "different-server":
        change["before"]["id"] = rule_id.replace("test-pg", "other-pg")
    elif invalid == "replacement":
        change["actions"] = ["delete", "create"]
    elif invalid in {"extra-update", "move"}:
        extra = {
            "address": "azurerm_postgresql_flexible_server.database",
            "type": "azurerm_postgresql_flexible_server",
            "change": {"actions": ["update" if invalid == "extra-update" else "no-op"]},
        }
        if invalid == "move":
            extra["previous_address"] = "azurerm_postgresql_flexible_server.old"
        io.runner.plan["resource_changes"].append(extra)
    elif invalid == "stale-state":
        io.current = state(io.current.resources, serial=1)

    assert checks.check_saved_plan() is (invalid is None)


def test_real_environment_stage_plan_is_admitted_without_apply(tmp_path):
    """Exercise the real Terraform representation that blocked the first live plan."""
    binary = os.environ.get("TIRODHAN_TEST_TERRAFORM_BINARY")
    if not binary:
        pytest.skip("Pinned Terraform supplied by lifecycle CI or explicit local invocation")
    io = MemoryObservations()
    # No Azure provider/backend: only local synthetic state and the built-in provider.
    fixture = {
        "terraform": {"required_version": "= 1.13.5"},
        "variable": {
            **{key: {"default": value} for key, value in CONFIG["terraform"].items()},
            "deployment_stage": {"type": "number"},
        },
        "output": {"deployment_stage": {"value": "${var.deployment_stage}"}},
        "resource": {"terraform_data": {"stage": {"input": "${var.deployment_stage}"}}},
    }
    (tmp_path / "main.tf.json").write_text(json.dumps(fixture))
    state = tmp_path / "terraform.tfstate"
    state.write_text(json.dumps(io.current.document))
    original = state.read_bytes()
    # Windows os.environ normalizes keys to uppercase; Terraform's TF_VAR suffix
    # is case sensitive. Preserve the exact spelling used by the Linux workflow.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() != "tf_var_deployment_stage"
    }
    environment["TF_VAR_deployment_stage"] = "0"

    def run(*arguments):
        """Run local Terraform inspection/planning only in the synthetic fixture."""
        result = subprocess.run(
            [binary, *arguments],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run("init", "-backend=false", "-input=false")
    run("plan", "-refresh=false", "-input=false", "-out=reviewed.tfplan")
    document = json.loads(run("show", "-json", "reviewed.tfplan"))
    assert document["variables"]["deployment_stage"]["value"] == "0"
    assert document["planned_values"]["outputs"]["deployment_stage"] == {
        "sensitive": False,
        "type": "number",
        "value": 0,
    }
    io.runner.plan = document
    plan = Path(tmp_path / "reviewed.tfplan")
    authorization = auth(
        "foundation-apply",
        plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
        state_fingerprint=io.current.fingerprint,
    )
    checks = Checks(authorization, io.config, {"plan": plan}, io)
    assert checks.check_saved_plan()
    assert state.read_bytes() == original
