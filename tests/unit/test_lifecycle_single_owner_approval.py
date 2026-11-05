"""The owner's manual approval remains mandatory, including for owner-started runs."""

from __future__ import annotations

import copy

import pytest
from lifecycle_fakes import CONFIG, MemoryObservations, auth

from tirodhan.deployment.lifecycle_checks import Checks


@pytest.fixture
def protected_execution(monkeypatch):
    environment = {
        "deployment_branch_policy": {"custom_branch_policies": True},
        "can_admins_bypass": False,
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [{"type": "User", "reviewer": {"id": 46423210}}],
            }
        ],
    }
    branches = {"branch_policies": [{"name": "main", "type": "branch"}]}
    observations = MemoryObservations()
    observations.runner.json = lambda *args: (
        branches if args[-1].endswith("/deployment-branch-policies") else environment
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "krishnadhoundiyal/tirodhan")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("TIRODHAN_EXECUTION_ENVIRONMENT", "nonprod")
    checks = Checks(auth("seed-secrets", execution_environment="nonprod"), CONFIG, {}, observations)
    return checks, environment, branches


def test_owner_can_approve_own_run_with_real_manual_main_only_gate(protected_execution):
    checks, _, _ = protected_execution
    assert checks.check_execution_authority()


@pytest.mark.parametrize(
    "case",
    [
        "no-rules",
        "no-manual-approval",
        "no-reviewers",
        "wrong-owner",
        "extra-reviewer",
        "duplicate-rule",
        "team-reviewer",
        "missing-reviewer-type",
        "malformed-reviewer",
        "missing-identity",
        "self-review-prevented",
        "self-review-unknown",
        "admin-bypass",
        "admin-bypass-unknown",
        "unrestricted-branches",
        "other-branch",
    ],
)
def test_missing_or_different_approval_policy_fails_closed(protected_execution, case):
    checks, environment, branches = protected_execution
    rule = environment["protection_rules"][0]
    if case == "no-rules":
        environment["protection_rules"] = None
    elif case == "no-manual-approval":
        environment["protection_rules"] = [{"type": "wait_timer", "wait_timer": 0}]
    elif case == "no-reviewers":
        rule["reviewers"] = []
    elif case == "wrong-owner":
        rule["reviewers"][0]["reviewer"]["id"] = 1
    elif case == "extra-reviewer":
        rule["reviewers"].append({"type": "User", "reviewer": {"id": 1}})
    elif case == "duplicate-rule":
        environment["protection_rules"].append(copy.deepcopy(rule))
    elif case == "team-reviewer":
        rule["reviewers"][0]["type"] = "Team"
    elif case == "missing-reviewer-type":
        del rule["reviewers"][0]["type"]
    elif case == "malformed-reviewer":
        rule["reviewers"] = [None]
    elif case == "missing-identity":
        rule["reviewers"][0]["reviewer"] = None
    elif case == "self-review-prevented":
        rule["prevent_self_review"] = True
    elif case == "self-review-unknown":
        del rule["prevent_self_review"]
    elif case == "admin-bypass":
        environment["can_admins_bypass"] = True
    elif case == "admin-bypass-unknown":
        del environment["can_admins_bypass"]
    elif case == "unrestricted-branches":
        environment["deployment_branch_policy"] = None
    elif case == "other-branch":
        branches["branch_policies"].append({"name": "feature", "type": "branch"})
    assert not checks.check_execution_authority()


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REPOSITORY", "someone/tirodhan"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("TIRODHAN_EXECUTION_ENVIRONMENT", "nonprod-plan"),
        ("GITHUB_ACTIONS", "false"),
    ],
)
def test_owner_approval_does_not_override_signed_execution_context(
    protected_execution, monkeypatch, key, value
):
    checks, _, _ = protected_execution
    monkeypatch.setenv(key, value)
    assert not checks.check_execution_authority()


@pytest.mark.parametrize(
    "operation,environment",
    [("foundation-plan", "nonprod-plan"), ("database-verify", "nonprod-database")],
)
def test_nonprod_owner_policy_does_not_change_independent_verifier_or_plan_context(
    protected_execution, monkeypatch, operation, environment
):
    checks, settings, _ = protected_execution
    settings["protection_rules"] = []
    checks.auth = auth(operation, execution_environment=environment)
    monkeypatch.setenv("TIRODHAN_EXECUTION_ENVIRONMENT", environment)
    assert checks.check_execution_authority()
