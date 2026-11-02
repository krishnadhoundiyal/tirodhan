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
