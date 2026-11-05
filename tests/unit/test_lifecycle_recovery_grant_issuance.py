"""Real deployment signatures preserve the already enrolled R grant without new privileges."""

import pytest
from lifecycle_fakes import MemoryObservations, assignments
from test_lifecycle_release_setup import (
    CODE,
    COMMIT,
    OID,
    ROID,
    SESSION,
    VOID,
    apply_request,
    operator_request,
    sign,
)
from test_lifecycle_release_setup import context as context

from tirodhan.deployment import release_setup as setup
from tirodhan.deployment.check_nonprod_bootstrap_rbac import READER_ROLE_ID
from tirodhan.deployment.deployment_admission import AdmissionDenied, digest, validate_authorization
from tirodhan.deployment.lifecycle_checks import Checks


@pytest.mark.parametrize(
    "kind",
    ["release", "apply", "seed-local", "seed-workflow", "log-local", "log-manual", "log-scheduled"],
)
def test_issued_deployment_authority_accepts_exact_existing_d_v_r_grants(context, tmp_path, kind):
    plan = apply_request(context, tmp_path) if kind == "apply" else None
    if kind not in {"release", "apply"}:
        operator_request(context, kind)
    token, _ = sign(context, plan=plan)
    _, policy, config, request = context
    operations = request["operations"]
    for op in operations:
        granted = validate_authorization(
            token,
            operation=op,
            policy=policy,
            config_hash=digest(config),
            code_hash=CODE,
            commit=COMMIT,
            run_id=SESSION,
        )
        assert granted.claims["bindings"][op]["recovery_principal_id"] == ROID
        obs = MemoryObservations()
        r_role = {
            "scope": obs.scope,
            "principalId": ROID,
            "roleDefinitionName": "Tirodhan NONPROD Vault Recovery Write test01",
        }
        rows = assignments() + [
            r_role,
            {
                "scope": obs.scope,
                "principalId": VOID,
                "roleDefinitionName": "Reader",
                "roleDefinitionId": READER_ROLE_ID,
                "principalType": "ServicePrincipal",
            },
        ]
        definitions = [
            {
                "assignableScopes": [obs.scope],
                "permissions": [
                    {
                        "actions": [
                            "Microsoft.KeyVault/vaults/read",
                            "Microsoft.KeyVault/vaults/write",
                            "Microsoft.Authorization/permissions/read",
                        ]
                    }
                ],
            }
        ]
        obs.runner.json = lambda *args, definitions=definitions, rows=rows: (
            definitions if args[:4] == ("az", "role", "definition", "list") else rows
        )
        checks = Checks(granted, config, {}, obs)
        assert checks.check_permissions()
        # A valid signature never excuses a different live recovery principal.
        r_role["principalId"] = OID
        with pytest.raises(ValueError, match="Recovery bootstrap grant"):
            checks.check_permissions()


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "not-a-principal"),
        ("id", OID),
        ("id", VOID),
        ("appId", "wrong-client"),
        ("tenant_id", "wrong-tenant"),
    ],
)
def test_issuance_rejects_missing_mismatched_or_shared_recovery_identity(context, field, value):
    context[3]["recovery_principal"][field] = value
    with pytest.raises(AdmissionDenied, match="RECOVERY_GRANT_PRINCIPAL_UNVERIFIED"):
        sign(context)


def test_configured_recovery_cannot_be_omitted_from_deployment_issuance(context):
    context[3].pop("recovery_principal")
    with pytest.raises(AdmissionDenied, match="RECOVERY_GRANT_PRINCIPAL_UNVERIFIED"):
        sign(context)


def test_uuid_casing_cannot_hide_a_shared_deployment_and_recovery_principal(context):
    _, _, _, request = context
    principal = "abcdefab-abcd-abcd-abcd-abcdefabcdef"
    request["principal_id"] = request["principal"]["id"] = principal
    request["recovery_principal"]["id"] = principal.upper()
    with pytest.raises(AdmissionDenied, match="RECOVERY_GRANT_PRINCIPAL_UNVERIFIED"):
        sign(context)


def test_no_recovery_configuration_cannot_be_promoted_to_an_extra_grant(context):
    context[2]["recovery_client_id"] = None
    context[3]["configuration_hash"] = digest(context[2])
    with pytest.raises(AdmissionDenied, match="RECOVERY_GRANT_BINDING_NOT_APPLICABLE"):
        sign(context)
    context[3].pop("recovery_principal")
    token, _ = sign(context)
    granted = validate_authorization(
        token,
        operation="foundation-plan",
        policy=context[1],
        config_hash=digest(context[2]),
        code_hash=CODE,
        commit=COMMIT,
        run_id=SESSION,
    )
    assert "recovery_principal_id" not in granted.claims["bindings"]["foundation-plan"]


def test_hold_blocks_recovery_grant_binding_before_private_key_unlock(context, monkeypatch):
    context[1]["release_enabled"] = False

    def forbidden(*args):
        pytest.fail("A deployment key was unlocked during hold")

    monkeypatch.setattr(setup, "load_private", forbidden)
    with pytest.raises(AdmissionDenied, match="DEPLOYMENT_HOLD"):
        sign(context)
