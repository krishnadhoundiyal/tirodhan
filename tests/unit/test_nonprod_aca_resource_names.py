from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import hcl2

NONPROD = Path("infra/terraform/nonprod")
ACA_NAME = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")


def _locals() -> dict[str, Any]:
    with (NONPROD / "locals.tf").open(encoding="utf-8") as source:
        parsed = hcl2.load(source)
    values: dict[str, Any] = {}
    for block in parsed["locals"]:
        values.update(block)
    return values


def test_aca_resource_aliases_cover_every_worker_and_scheduled_job() -> None:
    values = _locals()

    assert set(values["worker_resource_names"]) == set(values["workers"])
    # hcl2 preserves merge/conditional expressions rather than evaluating Terraform.
    jobs_expression = values["jobs"]
    assert isinstance(jobs_expression, str)
    job_names = set(re.findall(r"""["'](\w+)["']\s*:\s*["']\w+["']""", jobs_expression))
    assert set(values["scheduled_job_resource_names"]) == job_names
    assert "var.financial_inventory_schedule == null ? {}" in jobs_expression


def test_all_generated_aca_names_fit_azure_rules_at_maximum_suffix_length() -> None:
    values = _locals()
    prefix = f"tirodhan-np-{'x' * 10}"
    aliases = [
        "api",
        "migrate",
        *values["worker_resource_names"].values(),
        *values["scheduled_job_resource_names"].values(),
    ]
    names = [f"{prefix}-{alias}" for alias in aliases]

    assert len(names) == len(set(names))
    assert all(len(name) <= 32 for name in names)
    assert all(ACA_NAME.fullmatch(name) for name in names)


def test_terraform_uses_bounded_aliases_for_dynamic_aca_resources() -> None:
    container_apps = (NONPROD / "container_apps.tf").read_text(encoding="utf-8")
    jobs = (NONPROD / "jobs.tf").read_text(encoding="utf-8")

    worker_name = "${local.prefix}-${local.worker_resource_names[each.key]}"
    scheduled_name = "${local.prefix}-${local.scheduled_job_resource_names[each.key]}"

    assert worker_name in container_apps
    assert scheduled_name in jobs
    assert "${local.prefix}-${each.key}" not in container_apps
    assert "replace(each.key" not in jobs
