"""Exercise the actual destroy-apply shell branches with disposable fake tools."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(".github/workflows/nonprod-destroy.yml")
RESOURCE_GROUP = "tirodhan-np-tdnp01"
SUBSCRIPTION = "362d539c-c919-481e-97f5-1a9cda362747"


def _script(step_name: str) -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["destroy-apply"]["steps"]
    return next(step["run"] for step in steps if step.get("name") == step_name)


def _fake_tool(directory: Path, name: str, script: str) -> None:
    executable = directory / name
    executable.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + script, encoding="utf-8")
    executable.chmod(0o755)


def _environment(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    tools = tmp_path / "tools"
    tools.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(tools) + os.pathsep + env["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(tmp_path / "outputs"),
            "NONPROD_RESOURCE_GROUP": RESOURCE_GROUP,
            "ARM_SUBSCRIPTION_ID": SUBSCRIPTION,
            "STATE_ACCOUNT_NAME": "tdnpstatetdnp01",
        }
    )
    return tools, env


def _execute(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


POLLING_ERROR = (
    'Error: deleting Managed Environment (Subscription: "362d539c-c919-481e-97f5-1a9cda362747" '
    'Resource Group Name: "tirodhan-np-tdnp01" '
    'Managed Environment Name: "tirodhan-np-tdnp01-aca"): '
    "polling after Delete: internal-error: "
    'polling support for the Content-Type "" was not implemented: <nil>'
)


@pytest.mark.parametrize(
    ("message", "exit_code", "expected_success", "expected_reconcile"),
    [
        (POLLING_ERROR, 1, True, "true"),
        (POLLING_ERROR + "\nError: unrelated failure", 1, False, None),
        ("Error: denied by Azure", 1, False, None),
        (POLLING_ERROR.replace("tdnp01-aca", "other-aca"), 1, False, None),
        ("Apply complete!", 0, True, "false"),
    ],
)
def test_destroy_apply_only_defers_known_aca_polling_error(
    tmp_path: Path,
    message: str,
    exit_code: int,
    expected_success: bool,
    expected_reconcile: str | None,
) -> None:
    tools, env = _environment(tmp_path)
    _fake_tool(
        tools,
        "terraform",
        'printf "%s\\n" "$FAKE_TERRAFORM_MESSAGE"\nexit "$FAKE_TERRAFORM_EXIT"\n',
    )
    env["FAKE_TERRAFORM_MESSAGE"] = message
    env["FAKE_TERRAFORM_EXIT"] = str(exit_code)

    result = _execute(_script("Apply approved destroy plan"), tmp_path, env)
    assert (result.returncode == 0) is expected_success
    outputs = (tmp_path / "outputs").read_text() if (tmp_path / "outputs").exists() else ""
    assert outputs == (f"reconcile={expected_reconcile}\n" if expected_reconcile else "")


def _recovery(
    tmp_path: Path, *, azure_resources: str = "", remaining_deletes: bool = False
) -> tuple[subprocess.CompletedProcess[str], str]:
    tools, env = _environment(tmp_path)
    _fake_tool(tools, "az", 'printf "%s" "$FAKE_AZ_RESOURCES"\n')
    _fake_tool(tools, "sleep", ":\n")
    _fake_tool(
        tools,
        "terraform",
        'case "$1" in\n'
        "  plan) touch terminal-convergence.tfplan ;;\n"
        '  show) cat "$FAKE_PLAN_JSON" ;;\n'
        '  apply) printf "%s\\n" "$*" >> "$FAKE_APPLY_LOG" ;;\n'
        "  *) exit 90 ;;\n"
        "esac\n",
    )
    before = {
        "id": (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
            "/providers/Microsoft.App/managedEnvironments/tirodhan-np-tdnp01-aca"
        )
    }
    changes = (
        [
            {
                "address": "azurerm_container_app_environment.nonprod",
                "mode": "managed",
                "type": "azurerm_container_app_environment",
                "change": {"actions": ["delete"], "before": before},
            }
        ]
        if remaining_deletes
        else []
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"resource_changes": changes}), encoding="utf-8")
    env.update(
        {
            "FAKE_AZ_RESOURCES": azure_resources,
            "FAKE_PLAN_JSON": str(plan),
            "FAKE_APPLY_LOG": str(tmp_path / "applies"),
        }
    )
    result = _execute(
        _script("Verify and reconcile only an empty post-failure destroy"), tmp_path, env
    )
    apply_file = tmp_path / "applies"
    return result, apply_file.read_text() if apply_file.exists() else ""


def test_reconciles_only_when_azure_empty_and_terraform_plan_zero_delete(tmp_path: Path) -> None:
    result, applied = _recovery(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert applied == "apply -input=false terminal-convergence.tfplan\n"


def test_refuses_reconciliation_if_azure_has_remaining_resources(tmp_path: Path) -> None:
    result, applied = _recovery(tmp_path, azure_resources="unremoved Azure resource")
    assert result.returncode != 0
    assert "Azure application resources remain" in result.stderr
    assert not applied


def test_refuses_reconciliation_if_terraform_has_remaining_delete(tmp_path: Path) -> None:
    result, applied = _recovery(tmp_path, remaining_deletes=True)
    assert result.returncode != 0
    assert "requires zero remaining managed deletes" in result.stderr
    assert not applied


@pytest.mark.parametrize("initial_status", [0, 1])
def test_unverified_resource_blocks_reconciliation_after_apply(
    tmp_path: Path,
    initial_status: int,
) -> None:
    tools, env = _environment(tmp_path)
    _fake_tool(
        tools,
        "terraform",
        """
case "$1" in
  apply)
    printf '%s\n' "$*" >> "$APPLY_LOG"
    if [[ "$*" == *destroy.tfplan* ]]; then
      printf '%s\n' "$FAKE_TERRAFORM_MESSAGE"
      exit "$FAKE_TERRAFORM_EXIT"
    fi ;;
  show) printf '{}' ;;
  *) echo 'Reconciliation must not run after resource verification failure' >&2; exit 97 ;;
esac
""",
    )
    _fake_tool(
        tools,
        "python",
        """
if [[ "$*" == *tirodhan.deployment.destroy_aca_environment* ]]; then
  cat >/dev/null
  echo 'ENVIRONMENT_DELETION_DEADLINE_EXCEEDED' >&2
  exit 1
fi
exec "$REAL_PYTHON" "$@"
""",
    )
    env.update(
        FAKE_TERRAFORM_MESSAGE=POLLING_ERROR if initial_status else "Apply complete!",
        FAKE_TERRAFORM_EXIT=str(initial_status),
        REAL_PYTHON=sys.executable,
        APPLY_LOG=str(tmp_path / "applies"),
    )
    # Each Actions step is its own shell. The first step exits after classification.
    first = _execute(_script("Apply approved destroy plan"), tmp_path, env)
    assert first.returncode == 0
    script = "\n".join(
        [
            _script("Complete and verify approved ACA environment deletion"),
            _script("Verify and reconcile only an empty post-failure destroy"),
        ]
    )
    result = _execute(script, tmp_path, env)
    assert result.returncode != 0
    assert "ENVIRONMENT_DELETION_DEADLINE_EXCEEDED" in result.stderr
    assert (tmp_path / "applies").read_text() == "apply -input=false destroy.tfplan\n"
