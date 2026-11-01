"""Protected verifier identity, request lifecycle, isolation and fresh SQL facts."""

from __future__ import annotations

import copy
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Lock
from unittest.mock import AsyncMock, Mock

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from lifecycle_fakes import CONFIG, auth

from tirodhan.deployment import bootstrap_database, database_evidence, lifecycle_checks
from tirodhan.deployment import database_verification_request as requests
from tirodhan.deployment.deployment_admission import AdmissionDenied, digest
from tirodhan.deployment.lifecycle_checks import Checks

VOID = "00000000-0000-0000-0000-000000000011"
VCLIENT = "00000000-0000-0000-0000-000000000012"
VROLE = "tirodhan-np-test01-database-verifier"


@pytest.fixture
def consumer(monkeypatch, tmp_path):
    config = copy.deepcopy(CONFIG) | {
        "database_verification_client_id": VCLIENT,
        "database_verification_principal_id": VOID,
    }
    authorization = replace(auth("migration-apply"), configuration_hash=digest(config))
    authorization.claims["jti"] = "consumer-authorization"
    for key, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": requests.REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": authorization.commit,
        "GITHUB_WORKFLOW_REF": f"{requests.REPOSITORY}/{requests.DEPLOY_WORKFLOW}@refs/heads/main",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "migration-apply",
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    return authorization, config, requests.consumer_context(authorization)


def verifier(authorization):
    return replace(
        authorization,
        operation="database-verify",
        identity_kind="database_verification",
        principal_id=VOID,
        claims={
            "jti": "verifier-authorization",
            "bindings": {
                "database-verify": {
                    "database_verification_authorized": True,
                    "consumer_operations": ["migration-apply"],
                }
            },
        },
    )


class GithubFake:
    def __init__(self, request, *, conclusion="success", ambiguous=False, idle=False):
        self.request = request
        self.conclusion = conclusion
        self.ambiguous = ambiguous
        self.idle = idle
        self.started = False
        self.dispatches = 0
        self.deadline = time.monotonic() + 2
        self.lock = Lock()
        self.run = {
            "id": 41,
            "head_sha": request["commit"],
            "head_branch": "main",
            "path": ".github/workflows/" + requests.VERIFY_WORKFLOW,
            "event": "workflow_dispatch",
            "display_title": "SQL verification " + request["request_id"],
            "status": "completed",
            "conclusion": conclusion,
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as value:
            value.writestr("database-evidence.jwt", "new-signed-evidence")
        self.archive = archive.getvalue()

    def read(self, path):
        if path == "actions/runs/123":
            return {
                "head_sha": self.request["commit"],
                "head_branch": "main",
                "status": "in_progress",
                "path": requests.DEPLOY_WORKFLOW,
                "event": "workflow_dispatch",
                "run_attempt": 1,
                "repository": {"full_name": requests.REPOSITORY},
                "head_repository": {"full_name": requests.REPOSITORY},
            }
        if "/attempts/" in path:
            return {
                "total_count": 1,
                "jobs": [{"name": self.request["job"], "status": "in_progress"}],
            }
        if "/artifacts?" in path:
            return {
                "artifacts": [
                    {
                        "id": 42,
                        "name": "database-evidence-" + self.request["request_id"],
                        "expired": False,
                        "size_in_bytes": len(self.archive),
                    }
                ]
            }
        return {"workflow_runs": [self.run] if self.started and not self.idle else []}

    def dispatch(self, request):
        assert request == self.request
        with self.lock:
            self.dispatches += 1
            self.started = True
        if self.ambiguous:
            raise AdmissionDenied("DISPATCH_RESPONSE_LOST")

    def command(self, path):
        assert path.endswith("/artifacts/42/zip")
        return self.archive


@pytest.mark.parametrize("ambiguous", [False, True])
def test_request_reconciles_success_and_exact_replay_without_redispatch(
    monkeypatch, consumer, ambiguous
):
    authorization, _, request = consumer
    github = GithubFake(request, ambiguous=ambiguous)
    monkeypatch.setattr(requests, "Github", lambda: github)
    requests.refresh_database_evidence(authorization)
    requests.refresh_database_evidence(authorization)
    assert github.dispatches == 1
    assert requests.os.environ["TIRODHAN_DATABASE_PREREQUISITE_EVIDENCE"] == "new-signed-evidence"


def test_concurrent_same_request_dispatches_only_once(monkeypatch, consumer):
    authorization, _, request = consumer
    github = GithubFake(request)
    monkeypatch.setattr(requests, "Github", lambda: github)
    original_sleep = time.sleep
    monkeypatch.setattr(requests.time, "sleep", lambda _: original_sleep(0.01))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: requests.refresh_database_evidence(authorization), range(2)))
    assert github.dispatches == 1


@pytest.mark.parametrize("outcome", ["failure", "timeout", "bad-archive", "ambiguous-run"])
def test_failed_unknown_or_malformed_verification_never_returns_readiness(
    monkeypatch, consumer, outcome
):
    authorization, _, request = consumer
    github = GithubFake(
        request,
        conclusion="failure" if outcome == "failure" else "success",
        idle=outcome == "timeout",
    )
    if outcome == "timeout":
        github.deadline = time.monotonic() + 0.01
    if outcome == "bad-archive":
        github.archive = b"not-an-archive"
    if outcome == "ambiguous-run":
        original = github.read
        github.read = lambda path: (
            {"workflow_runs": [github.run, github.run]} if "/runs?" in path else original(path)
        )
    monkeypatch.setattr(requests, "Github", lambda: github)
    monkeypatch.delenv("TIRODHAN_DATABASE_PREREQUISITE_EVIDENCE", raising=False)
    with pytest.raises(AdmissionDenied):
        requests.refresh_database_evidence(authorization)
    assert "TIRODHAN_DATABASE_PREREQUISITE_EVIDENCE" not in requests.os.environ
    assert github.dispatches <= 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_branch", "feature"),
        ("head_sha", "other"),
        ("status", "completed"),
        ("event", "pull_request"),
        ("run_attempt", 2),
        ("path", ".github/workflows/nonprod-destroy.yml"),
    ],
)
def test_verifier_checks_actual_consumer_before_any_azure_authentication(consumer, field, value):
    authorization, _, request = consumer
    github = GithubFake(request)
    assert requests.validate_request(verifier(authorization), request, github) == request
    original = github.read
    github.read = lambda path: (
        original(path) | {field: value} if path == "actions/runs/123" else original(path)
    )
    with pytest.raises(AdmissionDenied, match="CONSUMER_NOT_TRUSTED"):
        requests.validate_request(verifier(authorization), request, github)
    assert github.dispatches == 0


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "verifier-admin",
        "verifier-oid",
        "verifier-elevated",
        "missing-grant",
        "unknown-role",
        "old-observations",
    ],
)
@pytest.mark.asyncio
async def test_verifier_uses_only_readonly_sql_and_requires_actual_facts(
    monkeypatch, tmp_path, consumer, invalid
):
    authorization, config, request = consumer
    v = verifier(authorization)
    target = {
        "host": "synthetic",
        "database": "tirodhan",
        "role": "runtime",
        "principal_id": "runtime-oid",
        "server_id": "/synthetic/server",
    }
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    policy = {
        "authorization_keys": {},
        "maximum_evidence_age_seconds": 300,
        "database_evidence_keys": {
            "test": {
                "principal_id": VOID,
                "public_key": public,
                "identity_kind": "database_verification",
            }
        },
    }
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/nonprod-release-policy.json").write_text(json.dumps(policy))
    monkeypatch.setattr(lifecycle_checks, "ROOT", tmp_path)
    monkeypatch.setattr(database_evidence, "signer", lambda _: ("test", private, 300))
    monkeypatch.setattr(bootstrap_database, "database_token", lambda: "not-to-be-persisted")
    if invalid == "old-observations":
        from types import SimpleNamespace

        monkeypatch.setattr(
            bootstrap_database, "time", SimpleNamespace(time=Mock(side_effect=[1000, 1301]))
        )
    emitted = []
    monkeypatch.setattr(database_evidence, "write_evidence", emitted.append)
    postgres, application = AsyncMock(), AsyncMock()
    for connection in (postgres, application):
        connection.transaction = Mock(return_value=AsyncMock())
    postgres.fetch.return_value = [
        {
            "rolename": "runtime",
            "objectId": "runtime-oid",
            "principalType": "service",
            "isAdmin": 0,
        },
        {
            "rolename": VROLE,
            "objectId": "wrong" if invalid == "verifier-oid" else VOID,
            "principalType": "service",
            "isAdmin": 1 if invalid == "verifier-admin" else 0,
        },
    ]

    async def scalar(query, *args):
        if query == "SELECT current_user":
            return VROLE
        if query.startswith("SELECT NOT"):
            return invalid != "verifier-elevated"
        if invalid == "unknown-role":
            return None
        return False

    postgres.fetchval.side_effect = scalar
    application.fetchval.side_effect = [True, invalid != "missing-grant", True, True, True]
    connect = AsyncMock(side_effect=[postgres, application])
    monkeypatch.setattr(bootstrap_database.asyncpg, "connect", connect)
    if invalid:
        with pytest.raises(RuntimeError, match="postconditions|expired"):
            await bootstrap_database.write_verified_evidence(
                target, v, database_user=VROLE, consumer_context=request
            )
        assert not emitted
    else:
        await bootstrap_database.write_verified_evidence(
            target, v, database_user=VROLE, consumer_context=request
        )
        monkeypatch.setenv("TIRODHAN_DATABASE_PREREQUISITE_EVIDENCE", emitted[0])
        monkeypatch.setattr(Checks, "database_target", lambda _: target)
        assert Checks(authorization, config, {}).check_database()
        facts = jwt.decode(emitted[0], options={"verify_signature": False})
        for mutation in (
            {"consumer_context": request | {"operation": "migration-start"}},
            {"checks": facts["checks"] | {"non_admin": 1}},
            {"iat": int(time.time()) - 301},
            {"observed_at": int(time.time()) - 301},
        ):
            token = jwt.encode(
                facts | mutation, private, algorithm="EdDSA", headers={"kid": "test"}
            )
            monkeypatch.setenv("TIRODHAN_DATABASE_PREREQUISITE_EVIDENCE", token)
            assert not Checks(authorization, config, {}).check_database()
        assert "not-to-be-persisted" not in emitted[0]
    for connection in (postgres, application):
        connection.transaction.assert_called_once_with(readonly=True)
        connection.execute.assert_not_awaited()
        connection.close.assert_awaited_once()
    assert all(c.kwargs["user"] == VROLE for c in connect.call_args_list)


def test_workload_and_secret_isolation_are_explicit():
    from pathlib import Path

    producer = yaml.safe_load(Path(".github/workflows/nonprod-database-verify.yml").read_text())
    consumer = yaml.safe_load(Path(".github/workflows/nonprod-deploy.yml").read_text())
    assert producer["concurrency"]["group"] != "nonprod-release"
    job = producer["jobs"]["verify"]
    assert job["environment"] == "nonprod-database" and job["needs"] == "release-admission"
    assert "NONPROD_DATABASE_EVIDENCE_PRIVATE_KEY_PEM" not in json.dumps(consumer)
    assert "NONPROD_DATABASE_PREREQUISITE_EVIDENCE" not in json.dumps(consumer)
    steps = job["steps"]
    login = next(i for i, s in enumerate(steps) if s.get("uses") == "azure/login@v2")
    assert "database_verifier authorize" in steps[login - 1]["run"]
    assert (
        steps[login]["with"]["client-id"] == "${{ vars.AZURE_NONPROD_DATABASE_VERIFY_CLIENT_ID }}"
    )


def test_verifier_key_cannot_reuse_release_key_even_with_different_pem_newlines():
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    policy = {
        "authorization_keys": {"release": public.replace("\n", "\r\n")},
        "database_evidence_keys": {"verifier": {"public_key": public}},
    }
    assert not database_evidence.verification_key_is_independent(policy, "verifier")
    policy["authorization_keys"] = {}
    assert database_evidence.verification_key_is_independent(policy, "verifier")


def test_verifier_erases_private_file_and_previous_report_after_denial(
    monkeypatch, tmp_path, consumer
):
    from pathlib import Path

    from tirodhan.deployment import database_verifier as module

    authorization, config, request = consumer
    monkeypatch.setattr(module, "authorize", lambda: (verifier(authorization), request))
    monkeypatch.setattr(module, "configuration", lambda: config)
    monkeypatch.setenv("DATABASE_EVIDENCE_PRIVATE_KEY_PEM", "must-not-survive")
    previous = tmp_path / "database-evidence.jwt"
    previous.write_text("must-not-be-uploaded")
    paths = []

    def deny(_):
        path = Path(module.os.environ["TIRODHAN_DATABASE_EVIDENCE_PRIVATE_KEY_FILE"])
        assert path.read_text() == "must-not-survive"
        assert "DATABASE_EVIDENCE_PRIVATE_KEY_PEM" not in module.os.environ
        paths.append(path)
        raise AdmissionDenied("MANDATORY_DENIAL")

    monkeypatch.setattr(module, "Controller", lambda *a: Mock(evaluate=deny))
    sql = AsyncMock()
    monkeypatch.setattr(module, "write_verified_evidence", sql)
    with pytest.raises(AdmissionDenied, match="MANDATORY_DENIAL"):
        module.verify()
    assert paths and all(not path.exists() for path in paths)
    assert not previous.exists()
    sql.assert_not_awaited()


@pytest.mark.parametrize("entry", ["admitted", "require_provisioning"])
def test_failed_verification_prevents_provider_controller_construction(
    monkeypatch, consumer, entry
):
    from tirodhan.deployment import foundation_reconcile, prerequisites

    authorization, _, _ = consumer
    module = foundation_reconcile if entry == "admitted" else prerequisites
    monkeypatch.setattr(module, "require_release_authorization", lambda _: authorization)
    config = Mock()
    monkeypatch.setattr(module, "configuration", config)
    monkeypatch.setattr(
        requests,
        "refresh_database_evidence",
        Mock(side_effect=AdmissionDenied("VERIFICATION_FAILED")),
    )
    with pytest.raises(AdmissionDenied, match="VERIFICATION_FAILED"):
        getattr(module, entry)("migration-apply")
    config.assert_not_called()


def test_database_check_runs_after_other_mandatory_observations(consumer):
    from tirodhan.deployment.prerequisites import CHECK_IDS, Controller

    authorization, _, _ = consumer
    observed = []

    def check(key):
        observed.append(key)
        return True

    checks = Mock(observed_state=None)
    checks.registry.return_value = {key: lambda k=key: check(k) for key in CHECK_IDS}
    Controller(authorization, checks).evaluate(authorization.operation)
    assert observed[-1] == "database"
