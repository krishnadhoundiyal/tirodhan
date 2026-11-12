"""Real signatures prove maintenance purpose isolation with the deployment hold intact."""

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from lifecycle_fakes import CLIENT, CONFIG, OID, SUB, TENANT, VAULT, MemoryObservations, auth
from test_lifecycle_release_setup import CODE, COMMIT, SESSION, sign
from test_lifecycle_release_setup import context as context

from tirodhan.deployment import (
    foundation_reconcile,
    recovery_execution,
    release_custody,
    release_setup,
)
from tirodhan.deployment.deployment_admission import (
    AdmissionDenied,
    digest,
    maintenance_target,
    validate_authorization,
)
from tirodhan.deployment.prerequisites import REQUIRED
from tirodhan.deployment.recovery_target import deleted_target

R = "00000000-0000-0000-0000-000000000022"
RCLIENT = "00000000-0000-0000-0000-000000000021"
APP = "00000000-0000-0000-0000-000000000023"
DELETED_AT = datetime.now(timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0
) - timedelta(days=2)


def test_recovery_timeout_cap_does_not_shorten_native_terraform_operations(monkeypatch):
    from types import SimpleNamespace

    from tirodhan.deployment.lifecycle_evidence import Runner

    observed = []

    def run(*args, **kwargs):
        observed.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tirodhan.deployment.lifecycle_evidence.subprocess.run", run)
    runner = Runner()
    runner.run("terraform", "apply", timeout=1800)
    runner.timeout_limit = 30
    runner.run("az", "rest", timeout=180)
    assert observed == [1800, 30]


def deleted_row():
    return {
        "name": "td-np-test01-kv",
        "id": f"/subscriptions/{SUB}/providers/Microsoft.KeyVault/locations/"
        "centralindia/deletedVaults/td-np-test01-kv",
        "properties": {
            "vaultId": VAULT,
            "location": "centralindia",
            "purgeProtectionEnabled": True,
            "deletionDate": DELETED_AT.isoformat(),
            "scheduledPurgeDate": (DELETED_AT + timedelta(days=90)).isoformat(),
        },
    }


def maintenance_context(context, kind):
    _, policy, config, request = context
    request.pop("recovery_principal", None)
    policy["release_enabled"] = False
    config["recovery_client_id"] = RCLIENT
    _, destination, identity, environment = release_setup.DESTINATIONS[kind]
    operation = next(iter(release_setup.expected_bindings(kind)))
    binding = release_setup.expected_bindings(kind)[operation] | {
        "target": maintenance_target(config),
        "recovery_client_id": RCLIENT,
        "recovery_principal_id": R,
        "operation_created_at": int(time.time()),
    }
    if kind == "enrollment":
        binding["recovery_app_object_id"] = APP
    else:
        binding["deleted_vault"] = deleted_target([deleted_row()], config)
    principal = OID if kind == "enrollment" else R
    request.update(
        kind=kind,
        destination=destination,
        identity_kind=identity,
        environment=environment,
        principal_id=principal,
        principal={"id": principal, "appId": RCLIENT, "tenant_id": TENANT},
        configuration_hash=digest(config),
        captured_at=int(time.time()),
        operations=[operation],
        maintenance_binding=binding,
    )
    return context


@pytest.mark.parametrize("kind", ["enrollment", "recovery"])
def test_held_policy_issues_only_exact_maintenance_purpose(context, kind):
    maintenance_context(context, kind)
    token, _ = sign(context)
    _, policy, config, request = context
    intended = request["operations"][0]
    for operation in REQUIRED:
        kwargs = dict(
            operation=operation,
            policy=policy,
            config_hash=digest(config),
            code_hash=CODE,
            commit=COMMIT,
            run_id=SESSION,
        )
        if operation == intended:
            result = validate_authorization(token, **kwargs)
            assert result.identity_kind == request["identity_kind"]
        else:
            with pytest.raises(AdmissionDenied):
                validate_authorization(token, **kwargs)
    assert policy["release_enabled"] is False


@pytest.mark.parametrize("kind", ["enrollment", "recovery"])
def test_maintenance_signature_expiry_and_context_reuse(context, kind):
    maintenance_context(context, kind)
    token, _ = sign(context)
    _, policy, config, request = context
    operation = request["operations"][0]
    kwargs = dict(
        operation=operation,
        policy=policy,
        config_hash=digest(config),
        code_hash=CODE,
        commit=COMMIT,
        run_id=SESSION,
    )
    for field, value in [
        ("config_hash", "different"),
        ("code_hash", "other"),
        ("commit", "b" * 40),
        ("run_id", "other-session"),
    ]:
        with pytest.raises(AdmissionDenied):
            validate_authorization(token, **(kwargs | {field: value}))
    private = release_custody.load_private(
        context[0] / "release.encrypted.pem", b"synthetic-test-passphrase-only"
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    for field, value in [
        ("exp", 1),
        ("sub", OID if kind == "recovery" else R),
        ("authorization_class", "DEPLOYMENT"),
        ("operations", [operation, "runtime-apply"]),
        ("aud", "tirodhan-nonprod-provisioning"),
    ]:
        changed = claims | {field: value}
        forged_context = jwt.encode(
            changed, private, algorithm="EdDSA", headers=jwt.get_unverified_header(token)
        )
        with pytest.raises(AdmissionDenied):
            validate_authorization(forged_context, **kwargs)
    with pytest.raises(AdmissionDenied):
        validate_authorization(token[:-10] + "invalidsig", **kwargs)


@pytest.mark.parametrize(
    "change", ["target", "principal", "unknown-binding", "local-recovery", "generation"]
)
def test_changed_maintenance_context_cannot_issue(context, change):
    maintenance_context(context, "recovery")
    binding = context[3]["maintenance_binding"]
    if change == "target":
        binding["target"]["subscription_id"] = R
    if change == "principal":
        binding["recovery_principal_id"] = OID
    if change == "unknown-binding":
        binding["allow_all"] = True
    if change == "local-recovery":
        binding["local_execution_authorized"] = True
    if change == "generation":
        binding["deleted_vault"]["deletion_date"] = "2026-10-01T00:00:00+00:00"
    with pytest.raises(AdmissionDenied):
        sign(context)


def test_renewed_recovery_retains_operation_id_but_cannot_reissue_same_capture(context):
    maintenance_context(context, "recovery")
    token, _ = sign(context)
    claims = jwt.decode(token, options={"verify_signature": False})
    with pytest.raises(AdmissionDenied, match="ALREADY_CLAIMED"):
        sign(context, output="duplicate.jwt")
    context[3]["expires_at"] += 60
    other, _ = sign(context, output="renewed.jwt")
    renewed = jwt.decode(other, options={"verify_signature": False})
    assert renewed["jti"] != claims["jti"]
    assert renewed["bindings"] == claims["bindings"]


@pytest.fixture
def recovery(monkeypatch):
    from tirodhan.deployment.recovery_enrollment import DISCOVERY, WRITE

    class IO(MemoryObservations):
        def __init__(self):
            super().__init__()
            self.config["recovery_client_id"] = RCLIENT
            self.vault_present = False
            self.status = "Succeeded"
            self.starts = 0
            self.lost = False
            self.accept = True
            self.deleted = deleted_row()
            self.runner.json = self.json
            self.runner.run = self.run

        def identity(self):
            return {"oid": R, "appid": RCLIENT, "tid": TENANT}

        def arm(self, resource_id, version):
            if resource_id.endswith("/providers/Microsoft.Authorization/permissions"):
                return {
                    "value": [{"actions": DISCOVERY + WRITE, "notActions": [], "dataActions": []}]
                }
            if resource_id == self.vault_id and self.vault_present:
                return {
                    "id": VAULT,
                    "location": "centralindia",
                    "properties": {
                        "tenantId": TENANT,
                        "enablePurgeProtection": True,
                        "enableRbacAuthorization": True,
                        "enableSoftDelete": True,
                        "softDeleteRetentionInDays": 90,
                        "provisioningState": self.status,
                    },
                }
            return None

        def json(self, *args):
            if args[:3] == (
                "gh",
                "api",
                "repos/krishnadhoundiyal/tirodhan/environments/nonprod-recovery",
            ):
                return {
                    "deployment_branch_policy": {"custom_branch_policies": True},
                    "protection_rules": [{"type": "required_reviewers", "reviewers": [{"id": 1}]}],
                }
            if args[0:2] == ("gh", "api"):
                return {"branch_policies": [{"name": "main"}]}
            if args[:3] == ("az", "keyvault", "list-deleted"):
                return [self.deleted]
            raise AssertionError(args)

        def run(self, *args, **kwargs):
            assert args[:3] == ("az", "keyvault", "recover") and "--no-wait" in args
            self.starts += 1
            if self.accept:
                self.vault_present = True
            if self.lost:
                raise AdmissionDenied("RESPONSE_LOST")
            return ""

    io = IO()
    a = replace(
        auth(
            "recover-vault",
            kind="recovery",
            execution_environment="nonprod-recovery",
            recovery_authorized=True,
            recovery_principal_id=R,
            recovery_client_id=RCLIENT,
            deleted_vault=deleted_target([deleted_row()], io.config),
            operation_created_at=int(time.time()),
        ),
        principal_id=R,
        configuration_hash=digest(io.config),
    )
    for key, value in {
        "DEPLOYMENT_SCOPE": io.scope,
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "krishnadhoundiyal/tirodhan",
        "GITHUB_REF": "refs/heads/main",
        "TIRODHAN_EXECUTION_ENVIRONMENT": "nonprod-recovery",
        "GITHUB_WORKFLOW_REF": "krishnadhoundiyal/tirodhan/"
        ".github/workflows/nonprod-reconcile.yml@refs/heads/main",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(foundation_reconcile, "require_release_authorization", lambda _: a)
    monkeypatch.setattr(foundation_reconcile, "configuration", lambda: io.config)
    monkeypatch.setattr(foundation_reconcile, "Observations", lambda _: io)
    monkeypatch.setattr(recovery_execution, "may_start", lambda _: True)
    monkeypatch.setattr(foundation_reconcile.time, "sleep", lambda _: None)
    return io, a


@pytest.mark.parametrize("lost", [False, True])
def test_recovery_success_lost_response_and_replay_leave_state_untouched(recovery, lost):
    io, a = recovery
    io.lost = lost
    before = io.current.fingerprint
    foundation_reconcile.recover_vault()
    foundation_reconcile.recover_vault()
    assert io.starts == 1 and io.current.fingerprint == before


def test_pending_recovery_resumes_by_observation_without_new_start(recovery, monkeypatch):
    io, _ = recovery
    io.vault_present = True
    io.status = "Creating"
    monkeypatch.setattr(
        foundation_reconcile.time, "sleep", lambda _: setattr(io, "status", "Succeeded")
    )
    foundation_reconcile.recover_vault()
    assert io.starts == 0


def test_unknown_recovery_remains_failure_and_prior_run_cannot_restart(recovery, monkeypatch):
    io, _ = recovery
    io.accept = False
    io.lost = True
    with pytest.raises(AdmissionDenied, match="OUTCOME_UNKNOWN"):
        foundation_reconcile.recover_vault()
    monkeypatch.setattr(recovery_execution, "may_start", lambda _: False)
    with pytest.raises(AdmissionDenied, match="OUTCOME_UNKNOWN"):
        foundation_reconcile.recover_vault()
    assert io.starts == 1


def test_changed_deletion_generation_blocks_before_recovery(recovery):
    io, a = recovery
    # The signed original binding stays valid; actual Azure metadata moves to a
    # different, independently valid deletion generation of the same vault name.
    io.deleted["properties"]["deletionDate"] = (DELETED_AT + timedelta(days=1)).isoformat()
    io.deleted["properties"]["scheduledPurgeDate"] = (DELETED_AT + timedelta(days=91)).isoformat()
    with pytest.raises(AdmissionDenied):
        foundation_reconcile.recover_vault()
    assert io.starts == 0


def test_process_interruption_after_provider_acceptance_resumes_without_new_start(
    recovery, monkeypatch
):
    io, _ = recovery
    original = io.runner.run

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(io.runner, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        foundation_reconcile.recover_vault()
    monkeypatch.setattr(recovery_execution, "may_start", lambda _: False)
    foundation_reconcile.recover_vault()
    assert io.starts == 1


@pytest.mark.parametrize("wrong", ["identity", "target", "expired"])
def test_recovery_rejects_identity_target_or_expired_authority(recovery, monkeypatch, wrong):
    io, a = recovery
    if wrong == "identity":
        monkeypatch.setattr(io, "identity", lambda: {"oid": OID, "appid": CLIENT, "tid": TENANT})
    if wrong == "target":
        a.claims["bindings"]["recover-vault"]["target"]["subscription_id"] = R
    if wrong == "expired":

        def expired(_):
            raise AdmissionDenied("EXPIRED")

        monkeypatch.setattr(foundation_reconcile, "require_release_authorization", expired)
    with pytest.raises(AdmissionDenied):
        foundation_reconcile.recover_vault()
    assert io.starts == 0


def test_recovery_rejects_mismatched_deployment_scope(recovery, monkeypatch):
    io, _ = recovery
    monkeypatch.setenv("DEPLOYMENT_SCOPE", io.scope + "-other")
    with pytest.raises(AdmissionDenied, match="PREREQUISITES_BLOCKED:target"):
        foundation_reconcile.recover_vault()
    assert io.starts == 0


@pytest.mark.parametrize(
    "prior,attempt,expected", [(False, 1, True), (True, 1, False), (False, 2, False)]
)
def test_trusted_workflow_history_fences_first_start(monkeypatch, prior, attempt, expected):
    a = auth(
        "recover-vault",
        kind="recovery",
        operation_created_at=int(time.time()) - 60,
        deleted_vault=deleted_target([deleted_row()], CONFIG),
    )
    for k, v in {
        "GITHUB_RUN_ID": "20",
        "GITHUB_RUN_ATTEMPT": str(attempt),
        "GITHUB_JOB": "reconcile",
    }.items():
        monkeypatch.setenv(k, v)
    current = {
        "id": 20,
        "head_sha": a.commit,
        "head_branch": "main",
        "path": recovery_execution.WORKFLOW,
        "event": "workflow_dispatch",
        "repository": {"full_name": recovery_execution.REPOSITORY},
        "head_repository": {"full_name": recovery_execution.REPOSITORY},
        "display_title": f"NONPROD recover-vault {a.run_id}",
        "run_attempt": attempt,
        "status": "in_progress",
    }

    class Github:
        def read(self, path):
            if path == "actions/runs/20":
                return current
            return {
                "total_count": 2 if prior else 1,
                "workflow_runs": [current] + ([current | {"id": 19}] if prior else []),
            }

    assert recovery_execution.may_start(a, Github()) is expected


@pytest.mark.parametrize(
    "failure",
    [
        "wrong-commit",
        "wrong-title",
        "foreign-repo",
        "missing-current",
        "incomplete",
        "duplicate",
        "oversized",
    ],
)
def test_uncertain_or_foreign_run_history_never_starts(monkeypatch, failure):
    a = auth(
        "recover-vault",
        kind="recovery",
        operation_created_at=int(time.time()) - 60,
        deleted_vault=deleted_target([deleted_row()], CONFIG),
    )
    monkeypatch.setenv("GITHUB_RUN_ID", "20")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_JOB", "reconcile")
    current = {
        "id": 20,
        "head_sha": a.commit,
        "head_branch": "main",
        "path": recovery_execution.WORKFLOW,
        "event": "workflow_dispatch",
        "repository": {"full_name": recovery_execution.REPOSITORY},
        "head_repository": {"full_name": recovery_execution.REPOSITORY},
        "display_title": f"NONPROD recover-vault {a.run_id}",
        "run_attempt": 1,
        "status": "in_progress",
    }
    if failure == "wrong-commit":
        current["head_sha"] = "b" * 40
    if failure == "wrong-title":
        current["display_title"] = "foreign"
    if failure == "foreign-repo":
        current["head_repository"] = {"full_name": "foreign/repo"}

    class Github:
        def read(self, path):
            if path == "actions/runs/20":
                return current
            rows = [] if failure == "missing-current" else [current]
            if failure == "duplicate":
                rows.append(current)
            count = 101 if failure == "oversized" else 2 if failure == "incomplete" else len(rows)
            return {"total_count": count, "workflow_runs": rows}

    with pytest.raises(AdmissionDenied):
        recovery_execution.may_start(a, Github())
