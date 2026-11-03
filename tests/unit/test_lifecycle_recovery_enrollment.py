"""Enrollment must remain available before recovery, without recovering the vault."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock
from urllib.parse import urlsplit

import pytest
from lifecycle_fakes import OID, MemoryObservations, assignments, auth

from tirodhan.deployment import foundation_reconcile, recovery_enrollment
from tirodhan.deployment.check_nonprod_bootstrap_rbac import READER_ROLE_ID, verify_bootstrap_rbac
from tirodhan.deployment.deployment_admission import AdmissionDenied, digest
from tirodhan.deployment.lifecycle_evidence import ObservationError

RCLIENT = "00000000-0000-0000-0000-000000000021"
R = "00000000-0000-0000-0000-000000000022"
APP = "00000000-0000-0000-0000-000000000023"
V = "00000000-0000-0000-0000-000000000024"


class Enrollment(MemoryObservations):
    def __init__(self):
        super().__init__()
        self.config["recovery_client_id"] = RCLIENT
        self.vault_present = False
        self.roles = {}
        self.grants = {}
        self.credentials = []
        self.writes = []
        self.lock = Lock()
        self.owner = True
        self.lost_response = False
        self.post_barrier = None
        self.conflicts = 0
        self.runner.json = self.json
        self.runner.run = self.run

    def arm(self, resource_id, version):
        if resource_id.endswith("/providers/Microsoft.Authorization/permissions"):
            return {"value": [{"actions": ["*"], "notActions": []}]}
        if resource_id in self.grants:
            return {"id": resource_id, "properties": copy.deepcopy(self.grants[resource_id])}
        for row in self.roles.values():
            if row["id"] == resource_id:
                return {
                    "id": resource_id,
                    "name": resource_id.rsplit("/", 1)[-1],
                    "properties": row,
                }
        return None

    def json(self, *args):
        if args[:4] == ("az", "ad", "sp", "show"):
            return {
                "id": R,
                "appId": RCLIENT,
                "accountEnabled": True,
                "servicePrincipalType": "Application",
                "passwordCredentials": [],
                "keyCredentials": [],
            }
        if args[:4] == ("az", "ad", "app", "show"):
            return {
                "id": APP,
                "appId": RCLIENT,
                "signInAudience": "AzureADMyOrg",
                "passwordCredentials": [],
                "keyCredentials": [],
            }
        if args[:5] == ("az", "ad", "app", "owner", "list"):
            return [{"id": OID}] if self.owner else []
        if args[:4] == ("az", "role", "definition", "list"):
            return [copy.deepcopy(self.roles[args[5]])] if args[5] in self.roles else []
        if args[:4] == ("az", "rest", "--method", "get"):
            return {"value": copy.deepcopy(self.credentials)}
        assert args[:4] == ("az", "rest", "--method", "put")
        url = args[args.index("--url") + 1]
        target = urlsplit(url).path
        props = json.loads(args[args.index("--body") + 1])["properties"]
        with self.lock:
            self.writes.append(target)
            if "/roleDefinitions/" in target:
                self.roles[props["roleName"]] = {"id": target, **copy.deepcopy(props)}
            else:
                assert "/roleAssignments/" in target
                self.grants[target] = copy.deepcopy(props)
        return {"id": target, "name": target.rsplit("/", 1)[-1], "properties": props}

    def run(self, *args, **kwargs):
        assert args[:4] == ("az", "rest", "--method", "post")
        url = args[args.index("--url") + 1]
        assert (
            url
            == f"https://graph.microsoft.com/v1.0/applications/{APP}/federatedIdentityCredentials"
        )
        if self.post_barrier is not None:
            self.post_barrier.wait(timeout=10)
        with self.lock:
            self.writes.append(url)
            if self.credentials:
                self.conflicts += 1
                # Graph requires unique name and issuer/subject within the app:
                # https://learn.microsoft.com/graph/api/resources/federatedidentitycredential
                raise ObservationError("Request_BadRequest")
            self.credentials.append(copy.deepcopy(recovery_enrollment.FEDERATION))
        if self.lost_response:
            raise AdmissionDenied("RESPONSE_LOST_AFTER_COMMIT")
        return ""


@pytest.fixture
def enrolled_context(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("DEPLOYMENT_SCOPE", raising=False)
    io = Enrollment()
    authorization = replace(
        auth(
            "enroll-recovery",
            kind="operator",
            recovery_enrollment_authorized=True,
            recovery_principal_id=R,
            recovery_client_id=RCLIENT,
            recovery_app_object_id=APP,
        ),
        configuration_hash=digest(io.config),
    )
    monkeypatch.setattr(
        foundation_reconcile, "require_release_authorization", lambda _: authorization
    )
    monkeypatch.setattr(foundation_reconcile, "configuration", lambda: io.config)
    monkeypatch.setattr(foundation_reconcile, "Observations", lambda _: io)
    return io, authorization


@pytest.mark.parametrize("lost_response", [False, True])
def test_actual_enrollment_before_soft_deleted_vault_recovery_replays(
    enrolled_context, lost_response
):
    io, _ = enrolled_context
    io.lost_response = lost_response
    recovery_enrollment.enroll()
    recovery_enrollment.enroll()
    assert len(io.roles) == 2 and len(io.grants) == 2 and len(io.credentials) == 1
    assert not io.vault_present and io.current.resources == {}
    assert all("Microsoft.KeyVault/vaults/" not in value for value in io.writes)


def test_concurrent_enrollment_converges_on_same_grants(enrolled_context):
    io, _ = enrolled_context
    io.post_barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: recovery_enrollment.enroll(), range(2)))
    assert len(io.roles) == 2 and len(io.grants) == 2 and len(io.credentials) == 1
    assert io.conflicts == 1
    assert sum("graph.microsoft.com" in target for target in io.writes) == 2


@pytest.mark.parametrize("effect", ["roleDefinitions", "roleAssignments"])
def test_committed_enrollment_write_with_lost_response_is_read_back(enrolled_context, effect):
    io, _ = enrolled_context
    original = io.runner.json
    lost = False

    def response(*args):
        nonlocal lost
        value = original(*args)
        if args[:4] == ("az", "rest", "--method", "put") and effect in str(value) and not lost:
            lost = True
            raise ObservationError("RESPONSE_LOST")
        return value

    io.runner.json = response
    recovery_enrollment.enroll()
    writes = list(io.writes)
    recovery_enrollment.enroll()
    assert lost and io.writes == writes
    assert len(io.grants) == 2 and len(io.roles) == 2 and len(io.credentials) == 1


def test_expiry_between_enrollment_writes_blocks_and_fresh_authority_resumes(
    enrolled_context, monkeypatch
):
    io, authorization = enrolled_context

    def current(_):
        if io.writes:
            raise AdmissionDenied("AUTHORIZATION_EXPIRED")
        return authorization

    monkeypatch.setattr(foundation_reconcile, "require_release_authorization", current)
    with pytest.raises(AdmissionDenied, match="EXPIRED"):
        recovery_enrollment.enroll()
    assert len(io.roles) == 1 and not io.grants and not io.credentials
    monkeypatch.setattr(
        foundation_reconcile, "require_release_authorization", lambda _: authorization
    )
    recovery_enrollment.enroll()
    assert len(io.roles) == 2 and len(io.grants) == 2 and len(io.credentials) == 1


def test_conflicting_existing_assignment_is_never_overwritten(enrolled_context):
    io, _ = enrolled_context
    recovery_enrollment.enroll()
    target = next(iter(io.grants))
    io.grants[target]["principalId"] = V
    writes = list(io.writes)
    with pytest.raises(AdmissionDenied, match="ASSIGNMENT_POSTCONDITION_FAILED"):
        recovery_enrollment.enroll()
    assert io.writes == writes and io.grants[target]["principalId"] == V


@pytest.mark.parametrize("invalid", ["disabled", "credentials", "multitenant", "verifier-client"])
def test_unapproved_recovery_identity_writes_nothing(enrolled_context, invalid):
    io, _ = enrolled_context
    original = io.runner.json

    def read(*args):
        value = original(*args)
        if args[:4] == ("az", "ad", "sp", "show"):
            if invalid == "disabled":
                value["accountEnabled"] = False
            if invalid == "credentials":
                value["passwordCredentials"] = [{"keyId": "unexpected"}]
        if args[:4] == ("az", "ad", "app", "show") and invalid == "multitenant":
            value["signInAudience"] = "AzureADMultipleOrgs"
        return value

    io.runner.json = read
    if invalid == "verifier-client":
        io.config["database_verification_client_id"] = RCLIENT
    with pytest.raises(AdmissionDenied):
        recovery_enrollment.enroll()
    assert not io.writes


def test_rejected_create_without_confirmed_credential_remains_failure(enrolled_context):
    io, _ = enrolled_context

    def rejected(*args, **kwargs):
        raise ObservationError("Request_BadRequest")

    io.runner.run = rejected
    with pytest.raises(AdmissionDenied, match="Request_BadRequest"):
        recovery_enrollment.enroll()
    assert not io.credentials


@pytest.mark.parametrize("form", ["provider", "subscription", "resource-group", "uppercase"])
def test_assignment_response_role_id_aliases_preserve_verified_identity(enrolled_context, form):
    io, _ = enrolled_context
    original = io.json

    def response(*args):
        value = original(*args)
        if isinstance(value, dict) and "/roleAssignments/" in str(value.get("id", "")):
            identifier = value["properties"]["roleDefinitionId"].rsplit("/", 1)[-1]
            prefix = "/providers/Microsoft.Authorization/roleDefinitions/"
            if form == "subscription":
                prefix = "/subscriptions/" + io.tf["subscription_id"] + prefix
            elif form == "resource-group":
                prefix = io.scope + prefix
            value["properties"] = value["properties"] | {"roleDefinitionId": prefix + identifier}
            if form == "uppercase":
                value["properties"]["roleDefinitionId"] = value["properties"][
                    "roleDefinitionId"
                ].upper()
        return value

    io.runner.json = response
    recovery_enrollment.enroll()
    recovery_enrollment.enroll()
    assert len(io.roles) == 2 and len(io.grants) == 2 and len(io.credentials) == 1


@pytest.mark.parametrize(
    "invalid", ["role", "malformed-role", "principal", "principal-type", "scope", "condition"]
)
def test_normalized_role_id_never_bypasses_other_assignment_postconditions(
    enrolled_context, invalid
):
    io, _ = enrolled_context
    original = io.json

    def response(*args):
        value = original(*args)
        if not isinstance(value, dict) or "/roleAssignments/" not in str(value.get("id", "")):
            return value
        properties = copy.deepcopy(value["properties"])
        if invalid == "role":
            properties["roleDefinitionId"] = (
                "/providers/Microsoft.Authorization/roleDefinitions/" + V
            )
        elif invalid == "malformed-role":
            properties["roleDefinitionId"] = (
                "/providers/Microsoft.Authorization/roleDefinitions/not-a-guid"
            )
        elif invalid == "principal":
            properties["principalId"] = V
        elif invalid == "principal-type":
            properties["principalType"] = "User"
        elif invalid == "scope":
            value["id"] = value["id"].replace(io.tf["subscription_id"], V)
        else:
            properties["condition"] = "unapproved-condition"
        return value | {"properties": properties}

    io.runner.json = response
    with pytest.raises(
        AdmissionDenied, match="RECOVERY_ENROLLMENT_ASSIGNMENT_POSTCONDITION_FAILED"
    ):
        recovery_enrollment.enroll()
    assert not io.credentials


@pytest.mark.parametrize(
    "invalid", ["missing-authority", "wrong-owner", "wrong-subject", "extra-trust"]
)
def test_incompatible_or_unauthorized_enrollment_writes_nothing(enrolled_context, invalid):
    io, authorization = enrolled_context
    if invalid == "missing-authority":
        authorization.claims["bindings"]["enroll-recovery"]["recovery_enrollment_authorized"] = (
            False
        )
    elif invalid == "wrong-owner":
        io.owner = False
    elif invalid == "wrong-subject":
        io.credentials = [recovery_enrollment.FEDERATION | {"subject": "unprotected"}]
    else:
        io.credentials = [recovery_enrollment.FEDERATION | {"name": "unapproved"}]
    with pytest.raises(AdmissionDenied):
        recovery_enrollment.enroll()
    assert not io.writes


@pytest.mark.parametrize("invalid", [None, "principal", "scope", "role", "extra-grant"])
def test_only_explicit_verifier_reader_extends_original_five_grants(invalid):
    values = assignments()
    reader = {
        "roleDefinitionName": "Reader",
        "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/" + READER_ROLE_ID,
        "scope": values[0]["scope"],
        "principalId": V,
        "principalType": "ServicePrincipal",
        "condition": None,
    }
    values.append(reader)
    if invalid == "principal":
        reader["principalId"] = R
    if invalid == "scope":
        reader["scope"] += "-foreign"
    if invalid == "role":
        reader["roleDefinitionId"] += "-different"
    if invalid == "extra-grant":
        values.append(reader | {"principalId": R})
    kwargs = {
        "expected_scope": values[0]["scope"],
        "expected_principal_id": OID,
        "expected_suffix": "test01",
        "verification_principal_id": V,
    }
    if invalid:
        with pytest.raises(ValueError):
            verify_bootstrap_rbac(values, **kwargs)
    else:
        verify_bootstrap_rbac(values, **kwargs)
