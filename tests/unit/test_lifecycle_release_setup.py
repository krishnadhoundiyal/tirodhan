"""Synthetic operator custody, real signatures, evidence failures and issuance races."""

import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from lifecycle_fakes import CONFIG, OID, state, write_plan

from tirodhan.deployment import release_custody as custody
from tirodhan.deployment import release_setup as setup
from tirodhan.deployment.deployment_admission import AdmissionDenied, digest, validate_authorization
from tirodhan.deployment.release_capture import enrollment_inventory

COMMIT = "a" * 40
SOURCE = "b" * 40
CODE = "c" * 64
SESSION = "00000000-0000-0000-0000-000000000099"
VCLIENT = "00000000-0000-0000-0000-000000000088"
VOID = "00000000-0000-0000-0000-000000000077"
RCLIENT = "00000000-0000-0000-0000-000000000021"
ROID = "00000000-0000-0000-0000-000000000022"
REPO = {"full_name": setup.REPOSITORY}
PASSWORD = b"synthetic-test-passphrase-only"


def review_evidence():
    return {
        "repository": setup.REPOSITORY,
        "target_commit": COMMIT,
        "contract_hash": CODE,
        "target_tree": "d" * 40,
        "source_tree": "d" * 40,
        "pull": {
            "number": 123,
            "merged": True,
            "merge_commit_sha": COMMIT,
            "head": {"sha": SOURCE, "repo": REPO},
            "base": {"ref": "main", "repo": REPO},
        },
        "workflow_runs": [
            {
                "id": index,
                "path": ".github/workflows/" + name,
                "head_sha": SOURCE,
                "status": "completed",
                "conclusion": "success",
                "event": "pull_request",
                "repository": REPO,
                "head_repository": REPO,
            }
            for index, name in enumerate(("pr-checks.yml", "nonprod-lifecycle-ci.yml"), start=1)
        ],
        "review": {
            "id": 123,
            "commit_id": SOURCE,
            "state": "COMMENTED",
            "submitted_at": "2026-11-02T00:00:00Z",
            "user": {"login": "coderabbitai[bot]", "id": 136622811},
        },
        "unresolved_threads": [],
    }


@pytest.fixture
def context(tmp_path, monkeypatch):
    for name in ("CI", "GITHUB_ACTIONS", "RUNNER_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    directory = tmp_path / "operator"
    public = custody.generate_pair(directory, PASSWORD, VOID)
    policy = {
        "schema_version": 1,
        "contract_version": "nonprod-lifecycle-v1",
        "release_enabled": True,
        "maximum_evidence_age_seconds": 300,
        **public,
    }
    config = copy.deepcopy(CONFIG) | {
        "recovery_client_id": RCLIENT,
        "recovery_principal_id": ROID,
        "database_verification_client_id": VCLIENT,
        "database_verification_principal_id": VOID,
    }
    request = {
        "schema_version": 1,
        "kind": "release",
        "secret_name": "NONPROD_RELEASE_AUTHORIZATION",
        "destination": "repository",
        "environment": "nonprod-plan",
        "identity_kind": "deployment",
        "repository": setup.REPOSITORY,
        "principal_id": OID,
        "principal": {
            "id": OID,
            "appId": config["deployment_client_id"],
            "tenant_id": config["terraform"]["tenant_id"],
        },
        "recovery_principal": {
            "id": ROID,
            "appId": RCLIENT,
            "tenant_id": config["terraform"]["tenant_id"],
        },
        "commit": COMMIT,
        "configuration_hash": digest(config),
        "contract_hash": CODE,
        "release_session": SESSION,
        "expires_at": int(time.time()) + 600,
        "captured_at": int(time.time()),
        "evidence": review_evidence(),
        "operations": sorted(setup.expected_bindings("release")),
    }
    return directory, policy, config, request


def sign(context, *, plan=None, output="release.jwt"):
    directory, policy, config, request = context
    audit = setup.issue(
        request,
        expected_request_hash=digest(request),
        policy=policy,
        config=config,
        code_hash=CODE,
        commit=COMMIT,
        private_key=directory / "release.encrypted.pem",
        password=PASSWORD,
        output=directory / output,
        plan=plan,
    )
    token = custody.private_read(directory / output).decode()
    setup.verify_token(token, request, policy, config, code_hash=CODE, commit=COMMIT, plan=plan)
    return token, audit


OPERATOR_PURPOSES = (
    "bootstrap",
    "database-bootstrap",
    "seed-local",
    "seed-workflow",
    "log-local",
    "log-manual",
    "log-scheduled",
)


def operator_request(context, kind):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _, policy, config, request = context
    secret, destination, identity, environment = setup.DESTINATIONS[kind]
    request.update(
        kind=kind,
        secret_name=secret,
        destination=destination,
        identity_kind=identity,
        environment=environment,
        operations=sorted(setup.expected_bindings(kind)),
    )
    if kind in {"bootstrap", "database-bootstrap"}:
        request["principal"]["appId"] = None
        request.pop("recovery_principal", None)
    if kind == "bootstrap":
        request["deployment_principal"] = {
            "id": "00000000-0000-0000-0000-000000000097",
            "appId": config["deployment_client_id"],
            "tenant_id": config["terraform"]["tenant_id"],
        }
    if kind == "database-bootstrap":
        config["terraform"]["postgres_admin_object_id"] = OID
        request["configuration_hash"] = digest(config)
        key = Ed25519PrivateKey.generate()
        kid = custody.key_id(key)
        policy["database_evidence_keys"][kid] = {
            "principal_id": OID,
            "identity_kind": "database",
            "public_key": custody.public_pem(key),
        }
        request["database_key_id"] = kid
    if kind.startswith("seed-"):
        request["secret_names"] = (
            ["ghcr-pull-pat"] if kind == "seed-workflow" else ["twofactor-api-key"]
        )


@pytest.mark.parametrize("kind", OPERATOR_PURPOSES)
def test_existing_operator_operation_profiles_have_real_bound_signatures(context, kind):
    operator_request(context, kind)
    token, _ = sign(context)
    _, policy, config, request = context
    op = request["operations"][0]
    granted = validate_authorization(
        token,
        operation=op,
        policy=policy,
        config_hash=digest(config),
        code_hash=CODE,
        commit=COMMIT,
        run_id=SESSION,
    )
    assert granted.identity_kind == setup.DESTINATIONS[kind][2]
    assert granted.claims["authorization_class"] == "DEPLOYMENT"
    if kind.startswith("seed-"):
        assert granted.claims["bindings"][op]["secret_names"] == request["secret_names"]
    if kind == "log-scheduled":
        assert granted.claims["bindings"][op]["schedule"] == "23 4 */3 * *"
    with pytest.raises(AdmissionDenied):
        validate_authorization(
            token,
            operation="recover-vault",
            policy=policy,
            config_hash=digest(config),
            code_hash=CODE,
            commit=COMMIT,
            run_id=SESSION,
        )


@pytest.mark.parametrize("kind", OPERATOR_PURPOSES)
def test_preparation_profiles_never_become_held_enrollment_authority(context, monkeypatch, kind):
    operator_request(context, kind)
    context[1]["release_enabled"] = False

    def forbidden(*args):
        pytest.fail("Signing key unlocked during hold")

    monkeypatch.setattr(setup, "load_private", forbidden)
    with pytest.raises(AdmissionDenied, match="DEPLOYMENT_HOLD"):
        sign(context)


@pytest.mark.parametrize("kind", OPERATOR_PURPOSES)
def test_signed_profile_executes_only_in_its_reviewed_context(context, monkeypatch, kind):
    from lifecycle_fakes import MemoryObservations

    from tirodhan.deployment.lifecycle_checks import Checks

    operator_request(context, kind)
    token, _ = sign(context)
    _, policy, config, request = context
    operation = request["operations"][0]
    authorization = validate_authorization(
        token,
        operation=operation,
        policy=policy,
        config_hash=digest(config),
        code_hash=CODE,
        commit=COMMIT,
        run_id=SESSION,
    )
    observations = MemoryObservations()
    observations.runner.json = lambda *args: {
        "deployment_branch_policy": {"protected_branches": True},
        "can_admins_bypass": False,
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [{"type": "User", "reviewer": {"id": 46423210}}],
            }
        ],
    }
    checks = Checks(authorization, config, {}, observations)
    is_local = setup.DESTINATIONS[kind][3] == "operator-local"
    monkeypatch.setenv("GITHUB_ACTIONS", "false" if is_local else "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", setup.REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("TIRODHAN_EXECUTION_ENVIRONMENT", setup.DESTINATIONS[kind][3])
    assert checks.check_execution_authority()
    monkeypatch.setenv("GITHUB_ACTIONS", "true" if is_local else "false")
    # The same operation/principal cannot promote local authority into a job,
    # or export a protected workflow token to a local operator context.
    assert not checks.check_execution_authority()


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "not-a-principal"),
        ("id", OID),
        ("appId", VCLIENT),
        ("tenant_id", VOID),
    ],
)
def test_bootstrap_cannot_rebind_the_observed_deployment_identity(context, field, value):
    operator_request(context, "bootstrap")
    context[3]["deployment_principal"][field] = value
    with pytest.raises(AdmissionDenied, match="BOOTSTRAP_DEPLOYMENT_PRINCIPAL_UNVERIFIED"):
        sign(context)


def test_signed_bootstrap_profile_satisfies_the_actual_bootstrap_controller(context, monkeypatch):
    from lifecycle_fakes import MemoryObservations

    from tirodhan.deployment.lifecycle_checks import Checks
    from tirodhan.deployment.prerequisites import Controller

    operator_request(context, "bootstrap")
    _, policy, config, request = context
    tf = config["terraform"]
    config["state_account"] = "tdnpstate" + tf["suffix"]
    config["state_resource_group"] = tf["resource_group_name"] + "-state"
    request["configuration_hash"] = digest(config)
    token, _ = sign(context)
    authorization = validate_authorization(
        token,
        operation="bootstrap",
        policy=policy,
        config_hash=digest(config),
        code_hash=CODE,
        commit=COMMIT,
        run_id=SESSION,
    )
    deployment_oid = request["deployment_principal"]["id"]
    assert (
        authorization.claims["bindings"]["bootstrap"]["deployment_principal_id"] == deployment_oid
    )
    observations = MemoryObservations()
    observations.config = copy.deepcopy(config)
    original_arm = observations.arm
    observations.arm = lambda resource, version: (
        {"value": [{"actions": ["*"], "notActions": []}]}
        if resource.endswith("/permissions")
        else original_arm(resource, version)
    )
    app_oid = "00000000-0000-0000-0000-000000000095"

    def directory(*args):
        if args[:4] == ("az", "ad", "sp", "show"):
            return {"id": deployment_oid, "appId": config["deployment_client_id"]}
        if args[:4] == ("az", "ad", "app", "show"):
            return {"id": app_oid, "appId": config["deployment_client_id"]}
        assert args[:4] == ("az", "ad", "app", "owner")
        return [{"id": OID}]

    observations.runner.json = directory
    for name, value in {
        "AZURE_SUBSCRIPTION_ID": tf["subscription_id"],
        "AZURE_TENANT_ID": tf["tenant_id"],
        "AZURE_LOCATION": tf["location"],
        "NONPROD_SUFFIX": tf["suffix"],
        "OPERATOR_OBJECT_ID": OID,
        "DEPLOYMENT_OBJECT_ID": deployment_oid,
        "FEDERATED_APP_OBJECT_ID": app_oid,
        "DEPLOYMENT_SCOPE": observations.scope,
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("RECOVERY_OBJECT_ID", "RECOVERY_FEDERATED_APP_OBJECT_ID", "KEY_VAULT_URL"):
        monkeypatch.delenv(name, raising=False)
    grant = Controller(authorization, Checks(authorization, config, {}, observations)).evaluate(
        "bootstrap"
    )
    assert set(grant.satisfied_checks) == {
        "target",
        "identity",
        "execution_authority",
        "bootstrap_authority",
    }


@pytest.mark.parametrize(
    "names",
    [
        None,
        [],
        ["arbitrary"],
        ["twofactor-api-key"] * 2,
        ["twofactor-api-key", "ghcr-pull-pat"],
        [123],
    ],
)
def test_seed_authorization_cannot_admit_unknown_partial_or_ambiguous_batch(context, names):
    operator_request(context, "seed-local")
    context[3]["secret_names"] = names
    with pytest.raises(AdmissionDenied, match="ISSUANCE_SECRET_BATCH_NOT_SUPPORTED"):
        sign(context)


def test_workflow_seed_is_limited_to_its_actual_ghcr_entrypoint(context):
    operator_request(context, "seed-workflow")
    context[3]["secret_names"] = ["twofactor-api-key"]
    with pytest.raises(AdmissionDenied, match="ISSUANCE_SECRET_BATCH_NOT_SUPPORTED"):
        sign(context)


def test_database_bootstrap_signer_cannot_substitute_verifier_identity(context):
    operator_request(context, "database-bootstrap")
    context[3]["principal_id"] = VOID
    context[3]["principal"]["id"] = VOID
    with pytest.raises(AdmissionDenied, match="ISSUANCE_PRINCIPAL_NOT_AUTHORIZED"):
        sign(context)


def test_scheduled_token_is_bound_to_actual_schedule_not_manual_authority(context, monkeypatch):
    from tirodhan.deployment.deployment_admission import validate_scheduled_context

    operator_request(context, "log-manual")
    token, _ = sign(context)
    _, policy, config, request = context
    granted = validate_authorization(
        token,
        operation="rotate-log-sas",
        policy=policy,
        config_hash=digest(config),
        code_hash=CODE,
        commit=COMMIT,
        run_id=SESSION,
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("TIRODHAN_SCHEDULE", "23 4 */3 * *")
    with pytest.raises(AdmissionDenied, match="SCHEDULED_OPERATION_NOT_AUTHORIZED"):
        validate_scheduled_context(granted)


@pytest.mark.parametrize("schedule,accepted", [("23 4 */3 * *", True), ("* * * * *", False)])
def test_recurring_token_only_accepts_the_committed_cron(context, monkeypatch, schedule, accepted):
    from tirodhan.deployment.deployment_admission import validate_scheduled_context

    operator_request(context, "log-scheduled")
    token, _ = sign(context)
    _, policy, config, _ = context
    granted = validate_authorization(
        token,
        operation="rotate-log-sas",
        policy=policy,
        config_hash=digest(config),
        code_hash=CODE,
        commit=COMMIT,
        run_id=SESSION,
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("TIRODHAN_SCHEDULE", schedule)
    if accepted:
        validate_scheduled_context(granted)
    else:
        with pytest.raises(AdmissionDenied, match="SCHEDULED_OPERATION_NOT_AUTHORIZED"):
            validate_scheduled_context(granted)


def test_two_independent_encrypted_keys_with_readback_and_idempotent_setup(context):
    directory, policy, _, _ = context
    release = custody.load_private(directory / "release.encrypted.pem", PASSWORD)
    database = custody.load_private(directory / "database.encrypted.pem", PASSWORD)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert custody.public_pem(release) != custody.public_pem(database)
    custody.generate_pair(directory, PASSWORD, VOID)
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
    assert b"BEGIN ENCRYPTED PRIVATE KEY" in before["release.encrypted.pem"]
    assert "PRIVATE KEY" not in json.dumps(policy)
    setup.validate_policy_keys(policy)


@pytest.mark.parametrize("kind", ["release", "database"])
def test_real_signature_and_exact_destination_without_secret_output(context, kind, capsys):
    directory, policy, config, request = context
    if kind == "database":
        request.pop("recovery_principal", None)
        request.update(
            kind=kind,
            secret_name="NONPROD_DATABASE_VERIFY_AUTHORIZATION",
            destination="nonprod-database",
            environment="nonprod-database",
            identity_kind="database_verification",
            principal_id=VOID,
            principal={"id": VOID, "appId": VCLIENT, "tenant_id": config["terraform"]["tenant_id"]},
            database_key_id=next(iter(policy["database_evidence_keys"])),
            operations=sorted(setup.expected_bindings(kind)),
        )
    token, audit = sign(context)
    assert audit["destination"] == request["destination"]
    assert "private" not in json.dumps(audit).lower()
    assert token not in json.dumps(audit)
    assert capsys.readouterr().out == ""
    with pytest.raises(AdmissionDenied):
        validate_authorization(
            token,
            operation="runtime-apply",
            policy=policy,
            config_hash=digest(config),
            code_hash=CODE,
            commit=COMMIT,
            run_id=SESSION,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "attacker/repo"),
        ("environment", "prod"),
        ("destination", "repository-other"),
        ("principal_id", VOID),
        ("commit", SOURCE),
        ("configuration_hash", "different"),
        ("contract_hash", "different"),
        ("identity_kind", "operator"),
        ("operations", ["runtime-apply"]),
        ("expires_at", 0),
        ("captured_at", 0),
    ],
)
def test_inconsistent_or_expired_inputs_cannot_issue(context, field, value):
    context[3][field] = value
    with pytest.raises(AdmissionDenied):
        sign(context)
    assert not (context[0] / "release.jwt").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "ci-missing",
        "ci-failed",
        "ci-wrong-commit",
        "review-missing",
        "review-fake-bot",
        "review-wrong-commit",
        "unresolved",
        "tree",
    ],
)
def test_ci_and_review_records_are_mandatory(context, mutation):
    evidence = context[3]["evidence"]
    if mutation == "ci-missing":
        evidence["workflow_runs"] = []
    elif mutation == "ci-failed":
        evidence["workflow_runs"][0]["conclusion"] = "failure"
    elif mutation == "ci-wrong-commit":
        evidence["workflow_runs"][0]["head_sha"] = COMMIT
    elif mutation == "review-missing":
        evidence["review"] = {}
    elif mutation == "review-fake-bot":
        evidence["review"]["user"]["id"] = 1
    elif mutation == "review-wrong-commit":
        evidence["review"]["commit_id"] = COMMIT
    elif mutation == "unresolved":
        evidence["unresolved_threads"] = [{"isResolved": False}]
    else:
        evidence["source_tree"] = "e" * 40
    with pytest.raises(AdmissionDenied):
        sign(context)


def test_hold_blocks_before_unlock_or_any_credential_write(context, monkeypatch):
    context[1]["release_enabled"] = False
    monkeypatch.setattr(setup, "load_private", lambda *a: pytest.fail("key opened while held"))
    with pytest.raises(AdmissionDenied, match="DEPLOYMENT_HOLD"):
        sign(context)
    assert not (context[0] / "issuance-journal").exists()


def test_signer_cannot_substitute_sql_key_for_release_key(context):
    directory, policy, config, request = context
    with pytest.raises(AdmissionDenied, match="RELEASE_SIGNER_NOT_ENROLLED"):
        setup.issue(
            request,
            expected_request_hash=digest(request),
            policy=policy,
            config=config,
            code_hash=CODE,
            commit=COMMIT,
            private_key=directory / "database.encrypted.pem",
            password=PASSWORD,
            output=directory / "release.jwt",
        )


def test_missing_sql_key_and_shared_key_are_rejected(context):
    directory, policy, _, _ = context
    database = next(iter(policy["database_evidence_keys"].values()))
    database["public_key"] = next(iter(policy["authorization_keys"].values()))
    with pytest.raises(AdmissionDenied, match="INDEPENDENT"):
        setup.validate_policy_keys(policy)
    assert not (directory / "release.jwt").exists()


def test_issuance_is_claimed_once_under_concurrency(context):
    def attempt(number):
        try:
            sign(context, output=f"release-{number}.jwt")
            return True
        except AdmissionDenied:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt, range(4))) == 1
    assert len(list((context[0] / "issuance-journal").glob("*.json"))) == 1


def test_existing_output_never_consumes_an_issuance_claim(context):
    path = context[0] / "release.jwt"
    custody.private_write(path, b"existing-private-output")
    with pytest.raises(AdmissionDenied, match="OUTPUT_ALREADY_EXISTS"):
        sign(context)
    assert path.read_bytes() == b"existing-private-output"
    assert not (context[0] / "issuance-journal").exists()


def test_crash_after_claim_blocks_automatic_resigning(context, monkeypatch):
    original = setup.private_write

    def crash(path, value):
        if path.suffix == ".jwt":
            raise OSError("synthetic-write-failure")
        original(path, value)

    monkeypatch.setattr(setup, "private_write", crash)
    with pytest.raises(OSError):
        sign(context)
    monkeypatch.setattr(setup, "private_write", original)
    with pytest.raises(AdmissionDenied, match="ALREADY_CLAIMED"):
        sign(context)


def apply_request(context, tmp_path):
    _, _, _, request = context
    current = state()
    plan = tmp_path / "runtime.tfplan"
    write_plan(plan, current)
    request.update(
        kind="apply",
        secret_name="NONPROD_APPLY_AUTHORIZATION",
        destination="nonprod",
        environment="nonprod",
        apply_operation="runtime-apply",
        operations=sorted(setup.expected_bindings("apply", "runtime-apply")),
        state={"kind": current.kind, "etag": current.etag, "document": current.document},
    )
    import hashlib

    request["plan_review"] = {
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "state_fingerprint": current.fingerprint,
        "lineage": current.document["lineage"],
        "serial": current.document["serial"],
        "etag": current.etag,
        "reviewed_by_operator": True,
    }
    return plan


def test_apply_binds_exact_saved_plan_and_authoritative_state(context, tmp_path):
    plan = apply_request(context, tmp_path)
    token, _ = sign(context, plan=plan)
    claims = jwt.decode(token, options={"verify_signature": False})
    binding = claims["bindings"]["runtime-apply"]
    assert binding["state_lineage"] == "lineage"
    assert binding["state_serial"] == 0
    assert binding["state_etag"] == "etag-0"
    assert not any(op.endswith("-plan") for op in claims["operations"])


@pytest.mark.parametrize("changed", ["plan", "lineage", "serial", "etag", "unreviewed", "missing"])
def test_changed_plan_or_state_is_not_authorized(context, tmp_path, changed):
    plan = apply_request(context, tmp_path)
    request = context[3]
    if changed == "plan":
        plan.write_bytes(plan.read_bytes() + b"changed")
    elif changed in {"lineage", "serial"}:
        request["state"]["document"][changed] = "different" if changed == "lineage" else 1
    elif changed == "etag":
        request["state"]["etag"] = "different"
    elif changed == "unreviewed":
        request["plan_review"]["reviewed_by_operator"] = False
    else:
        plan = None
    with pytest.raises(AdmissionDenied):
        sign(context, plan=plan)


def test_copied_token_cannot_cross_release_session(context):
    token, _ = sign(context)
    context[3]["release_session"] = "00000000-0000-0000-0000-000000000098"
    with pytest.raises(AdmissionDenied):
        setup.verify_token(token, context[3], context[1], context[2], code_hash=CODE, commit=COMMIT)


def test_revocation_invalidates_previously_valid_signature(context):
    token, _ = sign(context)
    policy = context[1]
    policy["authorization_keys"] = {}
    with pytest.raises(AdmissionDenied):
        setup.verify_token(token, context[3], policy, context[2], code_hash=CODE, commit=COMMIT)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sub", VOID),
        ("operations", ["runtime-apply"]),
        ("repository", "attacker/repo"),
        ("commit", SOURCE),
        ("configuration_hash", "changed"),
        ("run_id", "different"),
        ("exp", 0),
    ],
)
def test_even_signed_tokens_with_wrong_context_or_expiry_are_rejected(context, field, value):
    token, _ = sign(context)
    claims = jwt.decode(token, options={"verify_signature": False})
    claims[field] = value
    key = custody.load_private(context[0] / "release.encrypted.pem", PASSWORD)
    changed = jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": custody.key_id(key)})
    with pytest.raises(AdmissionDenied):
        setup.verify_token(
            changed, context[3], context[1], context[2], code_hash=CODE, commit=COMMIT
        )


def test_invalid_signature_is_rejected(context):
    token, _ = sign(context)
    parts = token.split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    with pytest.raises(AdmissionDenied):
        setup.verify_token(
            ".".join(parts), context[3], context[1], context[2], code_hash=CODE, commit=COMMIT
        )


def test_expired_capture_blocks_new_issuance_but_does_not_revoke_valid_token(context, monkeypatch):
    token, _ = sign(context)
    now = int(time.time())
    monkeypatch.setattr(setup.time, "time", lambda: now + 301)
    setup.verify_token(token, context[3], context[1], context[2], code_hash=CODE, commit=COMMIT)
    with pytest.raises(AdmissionDenied, match="EXPIRED"):
        sign(context, output="must-not-be-issued.jwt")


def test_proposals_retain_hold_and_refuse_destructive_overwrite(context):
    policy = context[1]
    proposal = setup.policy_proposal(policy, {}, [])
    assert proposal["release_enabled"] is False
    assert policy["release_enabled"] is True
    name = next(iter(policy["authorization_keys"]))
    with pytest.raises(AdmissionDenied, match="DIFFERENT_VALUE"):
        setup.policy_proposal(policy, {"authorization_keys": {name: "different"}}, [])


def test_wrong_password_and_nonprivate_storage_fail_closed(context, tmp_path):
    with pytest.raises(AdmissionDenied, match="CANNOT_BE_UNLOCKED"):
        custody.load_private(context[0] / "release.encrypted.pem", b"wrong")
    path = tmp_path / "public-directory"
    path.mkdir()
    if os.name != "nt":
        path.chmod(0o755)
        with pytest.raises(AdmissionDenied, match="NOT_PRIVATE"):
            custody.private_directory(path)


def test_custody_refuses_repository_paths_and_deployment_runners(context, monkeypatch):
    with pytest.raises(AdmissionDenied, match="OUTSIDE_REPOSITORY"):
        custody.private_directory(custody.ROOT / "private-keys")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(AdmissionDenied, match="TRUSTED_OPERATOR"):
        custody.load_private(context[0] / "release.encrypted.pem", PASSWORD)


def test_enrollment_presence_is_not_falsely_reported_as_valid():
    class Github:
        def get(self, path):
            if "variables" in path:
                return {"value": "synthetic-key-id"}
            return {"total_count": 1, "secrets": [{"name": "NONPROD_RELEASE_AUTHORIZATION"}]}

    result = enrollment_inventory(Github())
    assert (
        result["repository"]["NONPROD_RELEASE_AUTHORIZATION"]
        == "PRESENT_VALUE_UNVERIFIED_DO_NOT_OVERWRITE"
    )
    assert result["nonprod"]["NONPROD_APPLY_AUTHORIZATION"] == "MISSING"


def test_readiness_does_not_claim_deployment_authority(context):
    _, policy, config, _ = context
    policy["release_enabled"] = False
    report = setup.readiness(policy, config)
    assert report["status"] == "BLOCKED"
    assert report["provisioning_authorized"] is False
    assert "FRESH_SQL_EVIDENCE" in report["unverified"]
