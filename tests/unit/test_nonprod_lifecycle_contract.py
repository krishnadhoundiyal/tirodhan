"""Functional admission, adversarial evidence, state classification and effect boundaries."""

from __future__ import annotations

import copy
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from typing import Any
from unittest.mock import Mock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import given
from hypothesis import strategies as st

from tirodhan.deployment import foundation_reconcile as reconcile
from tirodhan.deployment.check_nonprod_bootstrap_rbac import (
    ADMIN_ROLE,
    BOOTSTRAP_ROLE_NAMES,
    expected_admin_condition,
    verify_bootstrap_rbac,
)
from tirodhan.deployment.deployment_admission import (
    AdmissionDenied,
    Authorization,
    digest,
    validate_authorization,
)
from tirodhan.deployment.destroy_preservation import disposable_inventory
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.lifecycle_evidence import (
    BackendState,
    ObservationError,
    Observations,
    classify_state,
    state_resources,
)
from tirodhan.deployment.prerequisites import (
    CHECK_IDS,
    REQUIRED,
    Controller,
    assert_manifest_complete,
)

SUB = "00000000-0000-0000-0000-000000000001"
OID = "00000000-0000-0000-0000-000000000002"
CLIENT = "00000000-0000-0000-0000-000000000003"
TENANT = "00000000-0000-0000-0000-000000000004"
SCOPE = f"/subscriptions/{SUB}/resourceGroups/tirodhan-np-test01"
VAULT = SCOPE + "/providers/Microsoft.KeyVault/vaults/td-np-test01-kv"
RESOURCE = SCOPE + "/providers/Microsoft.App/managedEnvironments/test-aca"
CONFIG = {
    "terraform": {
        "subscription_id": SUB,
        "tenant_id": TENANT,
        "resource_group_name": "tirodhan-np-test01",
        "location": "centralindia",
        "suffix": "test01",
        "runtime_env": {},
    },
    "state_account": "syntheticstate",
    "state_resource_group": "synthetic-state-rg",
    "deployment_client_id": CLIENT,
    "recovery_client_id": "separate-client",
}


@pytest.fixture(autouse=True)
def isolated_authorized_local_context(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_ACTIONS", "KEY_VAULT_URL", "DEPLOYMENT_SCOPE"):
        monkeypatch.delenv(name, raising=False)


def state(resources: dict[str, str] | None = None, *, serial: int = 0) -> BackendState:
    values = resources or {}
    document = {
        "version": 4,
        "lineage": "lineage",
        "serial": serial,
        "resources": [
            {
                "mode": "managed",
                "type": key.split(".")[0],
                "name": key.split(".")[1],
                "instances": [{"attributes": {"id": value}}],
            }
            for key, value in values.items()
        ],
        "outputs": {},
    }
    return BackendState("INITIALIZED", document, f"etag-{serial}", values)


def auth(operation: str, *, kind: str = "deployment", **binding: Any) -> Authorization:
    return Authorization(
        operation,
        OID,
        kind,
        digest(CONFIG),
        "code-hash",
        "a" * 40,
        "session",
        {"bindings": {operation: {"local_execution_authorized": True, **binding}}},
    )


def write_plan(path: Path, prior: BackendState) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("tfstate", json.dumps(prior.document))
        archive.writestr("tfstate-prev", json.dumps({"lineage": "", "serial": 0}))
        archive.writestr("tfplan", b"synthetic native plan payload")


def assignments() -> list[dict[str, Any]]:
    return [
        {
            "scope": SCOPE,
            "principalId": OID,
            "roleDefinitionName": name,
            "condition": expected_admin_condition() if name == ADMIN_ROLE else None,
            "conditionVersion": "2.0" if name == ADMIN_ROLE else None,
        }
        for name in (*BOOTSTRAP_ROLE_NAMES, "Tirodhan NONPROD Deployment test01")
    ]


class MemoryRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.plan: dict[str, Any] = {
            "variables": {
                **{k: {"value": v} for k, v in CONFIG["terraform"].items()},
                "deployment_stage": {"value": 0},
            },
            "resource_changes": [],
        }

    def json(self, *args: str) -> Any:
        self.commands.append(args)
        if args[:4] == ("az", "role", "assignment", "list"):
            return assignments()
        if args[:4] == ("az", "postgres", "flexible-server", "list-skus"):
            return [{"name": "Standard_B1ms"}]
        if args == ("terraform", "version", "-json"):
            return {
                "terraform_version": "1.13.5",
                "provider_selections": {"registry.terraform.io/hashicorp/azurerm": "5.8.0"},
            }
        if args[:3] == ("terraform", "show", "-json"):
            return self.plan
        raise AssertionError(args)

    def run(self, *args: str, **_: Any) -> str:
        self.commands.append(args)
        raise AssertionError("No mutation was expected: " + args[0])


class MemoryObservations(Observations):
    def __init__(self) -> None:
        super().__init__(copy.deepcopy(CONFIG))
        self.runner = MemoryRunner()  # type: ignore[assignment]
        self.current = state()
        self.active = {VAULT}
        self.vault_present = True

    def terraform_context(self) -> dict[str, Any]:
        return {
            "type": "azurerm",
            "config": {
                "storage_account_name": self.config["state_account"],
                "resource_group_name": self.config["state_resource_group"],
                "container_name": "tfstate",
                "key": "tirodhan/nonprod.tfstate",
                "use_azuread_auth": True,
            },
        }

    def identity(self) -> dict[str, Any]:
        return {"oid": OID, "appid": CLIENT, "tid": TENANT}

    def backend(self) -> BackendState:
        return self.current

    def inventory(self) -> set[str]:
        return self.active

    def arm(self, resource_id: str, version: str) -> dict[str, Any] | None:
        if resource_id.endswith("/providers/Microsoft.Authorization/permissions"):
            return {"value": [{"actions": ["*/read"], "notActions": []}]}
        if "/providers/Microsoft." in resource_id and resource_id.endswith(
            (
                "Microsoft.App",
                "Microsoft.Storage",
                "Microsoft.KeyVault",
                "Microsoft.ServiceBus",
                "Microsoft.ManagedIdentity",
                "Microsoft.DBforPostgreSQL",
            )
        ):
            return {"registrationState": "Registered"}
        if resource_id == VAULT and self.vault_present:
            return {
                "id": VAULT,
                "location": "centralindia",
                "properties": {
                    "tenantId": TENANT,
                    "enablePurgeProtection": True,
                    "enableRbacAuthorization": True,
                    "provisioningState": "Succeeded",
                },
            }
        return None


def test_real_foundation_checks_accept_initialized_clean_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("KEY_VAULT_URL", raising=False)
    monkeypatch.delenv("DEPLOYMENT_SCOPE", raising=False)
    io = MemoryObservations()
    authorization = auth("foundation-plan")
    grant = Controller(authorization, Checks(authorization, io.config, {}, io)).evaluate(
        "foundation-plan"
    )
    assert grant.state_fingerprint == io.current.fingerprint
    assert not any(command[:2] == ("az", "group") for command in io.runner.commands)


@pytest.mark.parametrize("kind", ["BACKEND_ABSENT", "STATE_ABSENT"])
def test_absent_or_new_backend_never_becomes_ready(kind: str) -> None:
    io = MemoryObservations()
    io.current = BackendState(kind, None, None, {})
    authorization = auth("foundation-plan")
    with pytest.raises(AdmissionDenied):
        Controller(authorization, Checks(authorization, io.config, {}, io)).evaluate(
            "foundation-plan"
        )


@pytest.mark.parametrize("operation", sorted(REQUIRED))
def test_missing_any_required_validator_never_succeeds(operation: str) -> None:
    authorization = auth(
        operation,
        kind={
            "bootstrap": "operator",
            "recover-vault": "recovery",
            "database-bootstrap": "database",
        }.get(operation, "deployment"),
    )
    for missing in REQUIRED[operation]:
        registry = {key: lambda: True for key in CHECK_IDS if key != missing}
        checks = Mock()
        checks.registry.return_value = registry
        with pytest.raises(AdmissionDenied, match="UNIMPLEMENTED"):
            Controller(authorization, checks).evaluate(operation)


@pytest.mark.parametrize("value", [False, None, "PASS", "UNIMPLEMENTED", "UNKNOWN", 1, []])
def test_non_boolean_or_missing_pass_is_not_satisfaction(value: Any) -> None:
    registry = {key: lambda: True for key in CHECK_IDS}
    registry["backend"] = lambda: value
    checks = Mock()
    checks.registry.return_value = registry
    with pytest.raises(AdmissionDenied, match="backend"):
        Controller(auth("foundation-plan"), checks).evaluate("foundation-plan")


@pytest.mark.parametrize("wrong_kind", ["operator", "database", "recovery"])
def test_privileged_identity_cannot_substitute_for_deployment(wrong_kind: str) -> None:
    with pytest.raises(AdmissionDenied, match="IDENTITY_BOUNDARY"):
        Controller(auth("foundation-plan", kind=wrong_kind), Mock()).evaluate("foundation-plan")


def test_manifest_omission_is_independent_of_validator_registry(tmp_path: Path) -> None:
    value = json.loads(Path("deploy/nonprod-prerequisites.json").read_text())
    value["operations"]["runtime-apply"].remove("database")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value))
    with pytest.raises(AdmissionDenied, match="MANIFEST_INCOMPLETE"):
        assert_manifest_complete(path)


@pytest.fixture
def signed_release() -> tuple[dict[str, Any], dict[str, Any], Ed25519PrivateKey]:
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    now = int(time.time())
    policy = {
        "schema_version": 1,
        "contract_version": "nonprod-lifecycle-v1",
        "release_enabled": True,
        "authorization_keys": {"test": public},
    }
    claims = {
        "iss": "tirodhan-nonprod-release",
        "aud": "tirodhan-nonprod-provisioning",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "sub": OID,
        "jti": "test-authorization",
        "repository": "krishnadhoundiyal/tirodhan",
        "commit": "a" * 40,
        "run_id": "session",
        "configuration_hash": digest(CONFIG),
        "contract_hash": "code-hash",
        "operations": ["foundation-plan"],
        "identity_kind": "deployment",
        "bindings": {"foundation-plan": {"local_execution_authorized": True}},
        "validation": {
            "ci_commit": "a" * 40,
            "contract_hash": "code-hash",
            "coderabbit_review": "synthetic-reviewed-commit",
        },
    }
    return policy, claims, private


def authorize(
    policy: dict[str, Any], claims: dict[str, Any], private: Ed25519PrivateKey
) -> Authorization:
    token = jwt.encode(claims, private, algorithm="EdDSA", headers={"kid": "test"})
    return validate_authorization(
        token,
        operation="foundation-plan",
        policy=policy,
        config_hash=digest(CONFIG),
        code_hash="code-hash",
        commit="a" * 40,
        run_id="session",
    )


def test_real_signature_and_operation_bindings(signed_release: Any) -> None:
    policy, claims, key = signed_release
    assert authorize(policy, claims, key).principal_id == OID
    with pytest.raises(AdmissionDenied):
        authorize(policy, claims, Ed25519PrivateKey.generate())


@pytest.mark.parametrize(
    "field,value",
    [
        ("commit", "b" * 40),
        ("configuration_hash", "wrong"),
        ("contract_hash", "wrong"),
        ("run_id", "another"),
        ("operations", []),
        ("repository", "foreign/repository"),
        ("exp", 1),
        ("identity_kind", "superuser"),
    ],
)
def test_signed_but_inapplicable_release_is_denied(
    signed_release: Any, field: str, value: Any
) -> None:
    policy, claims, key = signed_release
    claims[field] = value
    with pytest.raises(AdmissionDenied):
        authorize(policy, claims, key)


def test_boolean_enabled_without_pinned_signature_does_not_release(signed_release: Any) -> None:
    policy, claims, key = signed_release
    policy["authorization_keys"] = {}
    with pytest.raises(AdmissionDenied):
        authorize(policy, claims, key)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("new", "BACKEND_ABSENT"),
        ("uninitialized", "STATE_ABSENT"),
        ("clean", "CLEAN_INITIALIZED"),
        ("partial", "PARTIAL_OWNED"),
        ("owned", "ESTABLISHED_OWNED"),
        ("untracked", "CONFLICT"),
        ("missing-state-with-resources", "CONFLICT"),
    ],
)
def test_authoritative_state_classification(case: str, expected: str) -> None:
    backend = state()
    active = {VAULT}
    if case == "new":
        backend = BackendState("BACKEND_ABSENT", None, None, {})
    if case in {"uninitialized", "missing-state-with-resources"}:
        backend = BackendState("STATE_ABSENT", None, None, {})
    if case in {"owned", "partial"}:
        backend = state({"azurerm_container_app_environment.nonprod": RESOURCE})
    if case in {"owned", "untracked", "missing-state-with-resources"}:
        active.add(RESOURCE)
    assert classify_state(backend, active, app_scope=SCOPE, retained={VAULT}) == expected


@given(st.sets(st.integers(min_value=1, max_value=100), min_size=1))
def test_empty_outputs_never_hide_untracked_inventory(ids: set[int]) -> None:
    active = {RESOURCE + str(i) for i in ids}
    assert classify_state(state(), active, app_scope=SCOPE, retained={VAULT}) == "CONFLICT"


def test_actual_state_parser_includes_nested_and_ignores_only_data() -> None:
    document = {
        "version": 4,
        "lineage": "lineage",
        "serial": 2,
        "resources": [
            {"mode": "data", "type": "azurerm_key_vault", "name": "runtime", "instances": []},
            {
                "mode": "managed",
                "module": "module.nested",
                "type": "azurerm_container_app_environment",
                "name": "env",
                "instances": [{"index_key": "key", "attributes": {"id": RESOURCE}}],
            },
        ],
    }
    assert state_resources(document) == {
        'module.nested.azurerm_container_app_environment.env["key"]': RESOURCE
    }
    document["resources"][1]["instances"][0]["deposed"] = "old"
    with pytest.raises(ObservationError):
        state_resources(document)


def test_read_denial_cannot_be_backend_absence() -> None:
    io = Observations(CONFIG)
    io.runner = Mock()
    io.runner.json.side_effect = ObservationError("AuthorizationFailed")
    with pytest.raises(ObservationError, match="AuthorizationFailed"):
        io.backend()


def test_missing_vault_is_discovered_without_recovery() -> None:
    io = MemoryObservations()
    io.vault_present = False
    assert Checks(auth("foundation-plan"), io.config, {}, io).check_vault() is False
    assert io.runner.commands == []


def test_exact_vault_preservation_never_exempts_foreign_vaults() -> None:
    io = MemoryObservations()
    foreign = VAULT + "-foreign"
    assert disposable_inventory([VAULT, RESOURCE, foreign], io) == [RESOURCE, foreign]
    io.vault_present = False
    with pytest.raises(AdmissionDenied):
        disposable_inventory([VAULT], io)


@pytest.mark.parametrize("extra", ["unexpected-principal", "weakened-condition", "extra-grant"])
def test_recovery_rbac_extension_preserves_exact_original_grants(extra: str) -> None:
    rows = assignments()
    recovery = {
        "scope": SCOPE,
        "principalId": "recovery-oid",
        "roleDefinitionName": "Tirodhan NONPROD Vault Recovery Write test01",
    }
    rows.append(recovery)
    verify_bootstrap_rbac(
        rows,
        expected_scope=SCOPE,
        expected_principal_id=OID,
        expected_suffix="test01",
        recovery_principal_id="recovery-oid",
    )
    if extra == "unexpected-principal":
        recovery["principalId"] = "foreign"
    elif extra == "weakened-condition":
        next(r for r in rows if r["roleDefinitionName"] == ADMIN_ROLE)["condition"] = "true"
    else:
        rows.append({**recovery, "principalId": "foreign"})
    with pytest.raises(ValueError):
        verify_bootstrap_rbac(
            rows,
            expected_scope=SCOPE,
            expected_principal_id=OID,
            expected_suffix="test01",
            recovery_principal_id="recovery-oid",
        )


def test_saved_plan_rejects_state_change_and_unexpected_provisioning(tmp_path: Path) -> None:
    path = tmp_path / "approved.tfplan"
    write_plan(path, state())
    import hashlib

    io = MemoryObservations()
    authorization = auth(
        "foundation-apply",
        plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        state_fingerprint=io.current.fingerprint,
    )
    checks = Checks(authorization, io.config, {"plan": path}, io)
    assert checks.check_saved_plan()
    io.current = state(serial=1)
    assert not checks.check_saved_plan()
    io.current = state()
    io.runner.plan["resource_changes"] = [
        {"mode": "managed", "type": "azurerm_key_vault", "change": {"actions": ["create"]}}
    ]
    assert not checks.check_saved_plan()


def test_transfer_requires_snapshot_readback_before_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    io = MemoryObservations()
    effect = Mock()
    io.runner.run = effect
    monkeypatch.setattr(
        reconcile, "require_release_authorization", lambda _: auth("transfer-vault")
    )
    monkeypatch.setattr(reconcile, "configuration", lambda: CONFIG)
    monkeypatch.setattr(reconcile, "Observations", lambda _: io)
    monkeypatch.setattr(
        reconcile, "read_backup", Mock(side_effect=ObservationError("BACKUP_MISSING"))
    )
    backup = tmp_path / "backup.json"
    backup.write_text('{"snapshot":"unverified"}')
    with pytest.raises(AdmissionDenied):
        reconcile.transfer_vault(tmp_path / "plan", backup)
    effect.assert_not_called()


def test_concurrent_transfer_uses_saved_plan_serial_fence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both controllers pass observations; only native saved-plan CAS may commit."""
    barrier, lock = Barrier(2), Lock()
    current = {"serial": 0, "mutations": 0}
    baseline = state({"azurerm_key_vault.runtime": VAULT})
    plan = tmp_path / "transfer.tfplan"
    write_plan(plan, baseline)
    backup = tmp_path / "backup.json"
    backup.write_text('{"snapshot":"verified"}')
    import hashlib

    authorization = auth(
        "transfer-vault",
        state_fingerprint=baseline.fingerprint,
        plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
        ownership_transfer_authorized=True,
    )

    class TransferObservations(MemoryObservations):
        def __init__(self) -> None:
            super().__init__()
            self.current = baseline
            self.runner.plan = {
                "variables": copy.deepcopy(self.runner.plan["variables"]),
                "resource_changes": [
                    {
                        "address": "azurerm_key_vault.runtime",
                        "mode": "managed",
                        "type": "azurerm_key_vault",
                        "change": {"actions": ["forget"], "before": {"id": VAULT}},
                    }
                ],
            }
            self.runner.run = self.apply

        def backend(self) -> BackendState:
            if current["serial"]:
                return state(serial=current["serial"])
            return baseline

        def apply(self, *args: str, **_: Any) -> str:
            assert args[:2] == ("terraform", "apply")
            barrier.wait(timeout=5)
            with lock:
                if current["serial"] != 0:
                    raise ObservationError("SAVED_PLAN_IS_STALE")
                current["serial"] = 1
                current["mutations"] += 1
            return ""

    monkeypatch.setattr(reconcile, "require_release_authorization", lambda _: authorization)
    monkeypatch.setattr(reconcile, "configuration", lambda: CONFIG)
    monkeypatch.setattr(reconcile, "Observations", lambda _: TransferObservations())
    monkeypatch.setattr(reconcile, "read_backup", lambda *_: baseline.document)

    def execute() -> str:
        try:
            reconcile.transfer_vault(plan, backup)
            return "committed"
        except AdmissionDenied:
            return "blocked"

    # The serialized state fingerprint is part of the real backup prerequisite.
    backup.write_text(
        json.dumps({"snapshot": "verified", "state_fingerprint": baseline.fingerprint})
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert sorted(results) == ["blocked", "committed"]
    assert current["mutations"] == 1


def test_bootstrap_never_recovers_a_soft_deleted_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    io = MemoryObservations()
    io.config["state_resource_group"] = io.tf["resource_group_name"] + "-state"
    io.config["state_account"] = "tdnpstate" + io.tf["suffix"]
    io.vault_present = False
    authorization = auth("bootstrap", kind="operator", deployment_principal_id="deployment-oid")
    for name, value in {
        "AZURE_SUBSCRIPTION_ID": SUB,
        "AZURE_TENANT_ID": TENANT,
        "AZURE_LOCATION": io.tf["location"],
        "NONPROD_SUFFIX": io.tf["suffix"],
        "OPERATOR_OBJECT_ID": OID,
        "DEPLOYMENT_OBJECT_ID": "deployment-oid",
    }.items():
        monkeypatch.setenv(name, value)
    io.runner.json = lambda *args: [{"name": VAULT.rsplit("/", 1)[-1]}]
    with pytest.raises(AdmissionDenied, match="SEPARATELY_AUTHORIZED_VAULT_RECOVERY_REQUIRED"):
        Checks(authorization, io.config, {}, io).check_bootstrap_authority()
    assert io.runner.commands == []
    assert "az keyvault recover" not in Path("deploy/bootstrap/bootstrap.sh").read_text()


def test_wrong_terraform_context_stops_before_other_observations() -> None:
    observed = []

    def run(key: str) -> bool:
        observed.append(key)
        return key != "terraform_context"

    checks = Mock()
    checks.registry.return_value = {key: lambda k=key: run(k) for key in CHECK_IDS}
    with pytest.raises(AdmissionDenied, match="terraform_context"):
        Controller(auth("foundation-apply"), checks).evaluate("foundation-apply")
    assert observed == ["target", "identity", "execution_authority", "terraform_context"]
