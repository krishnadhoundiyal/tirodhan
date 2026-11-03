"""Authenticated collector boundaries: failed evidence and unrelated artifacts cannot pass."""

import copy
import io
import time
import zipfile

import pytest
from test_lifecycle_release_setup import CODE, COMMIT, SESSION, SOURCE, review_evidence

from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.release_capture import capture_plan_artifact, collect_review


@pytest.mark.parametrize("kind", ["enrollment", "recovery"])
def test_actual_maintenance_capture_uses_real_identity_and_exact_target(monkeypatch, kind):
    from lifecycle_fakes import CLIENT, CONFIG, OID, TENANT
    from test_lifecycle_maintenance_authorization import APP, RCLIENT, R, deleted_row

    from tirodhan.deployment import release_capture, release_setup
    from tirodhan.deployment.deployment_admission import maintenance_target
    from tirodhan.deployment.lifecycle_evidence import Observations

    config = copy.deepcopy(CONFIG) | {"recovery_client_id": RCLIENT}
    monkeypatch.setattr(release_capture, "contract_digest", lambda: CODE)
    monkeypatch.setattr(
        Observations, "identity", lambda _: {"oid": OID, "appid": CLIENT, "tid": TENANT}
    )
    monkeypatch.setattr(Observations, "arm", lambda *_: None)

    class Runner:
        def run(self, *args):
            if "status" in args:
                return ""
            return "d" * 40 if "HEAD^{tree}" in args else COMMIT

        def json(self, *args):
            if args[:3] == ("az", "account", "show"):
                return {"tenantId": TENANT}
            if args[:4] == ("az", "ad", "sp", "show"):
                return {"id": R, "appId": RCLIENT}
            if args[:4] == ("az", "ad", "app", "show"):
                return {"id": APP, "appId": RCLIENT}
            if args[:5] == ("az", "ad", "app", "owner", "list"):
                return [{"id": OID}]
            if args[:3] == ("az", "keyvault", "list-deleted"):
                return [deleted_row()]
            raise AssertionError(args)

    request = release_capture.prepare(
        kind,
        session=SESSION,
        expires_at=int(time.time()) + 600,
        review_pr=123,
        github=ReviewGithub(),
        runner=Runner(),
        config=config,
    )
    assert request["principal_id"] == (OID if kind == "enrollment" else R)
    assert request["maintenance_binding"]["target"] == maintenance_target(config)
    assert request["maintenance_binding"]["recovery_principal_id"] == R
    policy = {
        "schema_version": 1,
        "contract_version": "nonprod-lifecycle-v1",
        "release_enabled": False,
        "authorization_keys": {"present": "not-loaded-until-signing"},
        "maximum_evidence_age_seconds": 300,
    }
    monkeypatch.setattr(release_setup, "validate_policy_keys", lambda _: None)
    bindings, _ = release_setup.validated_request(
        request, policy, config, code_hash=CODE, commit=COMMIT
    )
    assert bindings[next(iter(bindings))]["operation_id"]


class ReviewGithub:
    def __init__(self):
        self.evidence = review_evidence()
        self.reads = []

    def get(self, path):
        self.reads.append(path)
        if path == "commits/main":
            return {"sha": COMMIT}
        if path.startswith("pulls/") and "/reviews" in path:
            return [self.evidence["review"]]
        if path.startswith("pulls/"):
            return self.evidence["pull"]
        if path.startswith("git/commits/"):
            return {"tree": {"sha": self.evidence["target_tree"]}}
        return {"total_count": 2, "workflow_runs": self.evidence["workflow_runs"]}

    def threads(self, number):
        return self.evidence["unresolved_threads"]


def test_collector_requires_real_records_for_both_workflows_and_review():
    github = ReviewGithub()
    evidence = collect_review(github, 123, COMMIT, CODE)
    assert evidence["review"]["id"] == 123
    assert any(f"head_sha={SOURCE}" in path for path in github.reads)


@pytest.mark.parametrize(
    "failure",
    ["missing-ci", "failed-ci", "wrong-head", "wrong-repository", "unmerged", "unresolved"],
)
def test_collector_rejects_bad_ci_or_review(failure):
    github = ReviewGithub()
    evidence = github.evidence
    if failure == "missing-ci":
        evidence["workflow_runs"] = []
    elif failure == "failed-ci":
        evidence["workflow_runs"][0]["conclusion"] = "failure"
    elif failure == "wrong-head":
        evidence["workflow_runs"][0]["head_sha"] = COMMIT
    elif failure == "wrong-repository":
        evidence["pull"]["head"] = copy.deepcopy(evidence["pull"]["head"])
        evidence["pull"]["head"]["repo"]["full_name"] = "attacker/repo"
    elif failure == "unmerged":
        evidence["pull"]["merged"] = False
    else:
        evidence["unresolved_threads"] = [{"isResolved": False}]
    with pytest.raises(AdmissionDenied):
        collect_review(github, 123, COMMIT, CODE)


class ArtifactGithub:
    def __init__(self, plan):
        self.artifact = {
            "expired": False,
            "name": "migration-plan",
            "workflow_run": {"id": 10, "head_sha": COMMIT},
        }
        self.run = {
            "head_sha": COMMIT,
            "head_branch": "main",
            "run_attempt": 1,
            "path": ".github/workflows/nonprod-deploy.yml",
            "event": "workflow_dispatch",
            "repository": {"full_name": "krishnadhoundiyal/tirodhan"},
            "head_repository": {"full_name": "krishnadhoundiyal/tirodhan"},
            "status": "in_progress",
        }
        self.jobs = {
            "total_count": 1,
            "jobs": [{"name": "migration-plan", "conclusion": "success"}],
        }
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("migration.tfplan", plan.read_bytes())
        self.archive = data.getvalue()

    def get(self, path):
        if "artifacts/" in path:
            return self.artifact
        if "/jobs" in path:
            return self.jobs
        return self.run

    def download_artifact(self, artifact_id):
        assert artifact_id == 11
        return self.archive


@pytest.mark.parametrize("status", ["in_progress", "waiting"])
def test_exact_plan_artifact_can_be_reviewed_while_apply_waits(tmp_path, status):
    plan = tmp_path / "migration.tfplan"
    plan.write_bytes(b"synthetic native plan bytes")
    github = ArtifactGithub(plan)
    github.run["status"] = status
    result = capture_plan_artifact(
        github, 11, plan, "migration-apply", commit=COMMIT, session=SESSION
    )
    assert result["artifact_id"] == 11


@pytest.mark.parametrize(
    "failure", ["bytes", "commit", "branch", "expired", "job", "workflow", "foreign", "completed"]
)
def test_foreign_changed_or_unverified_plan_artifacts_fail(tmp_path, failure):
    plan = tmp_path / "migration.tfplan"
    plan.write_bytes(b"synthetic native plan bytes")
    github = ArtifactGithub(plan)
    if failure == "bytes":
        plan.write_bytes(b"different plan")
    elif failure == "commit":
        github.run["head_sha"] = SOURCE
    elif failure == "branch":
        github.run["head_branch"] = "untrusted"
    elif failure == "expired":
        github.artifact["expired"] = True
    elif failure == "job":
        github.jobs["jobs"][0]["conclusion"] = "failure"
    elif failure == "workflow":
        github.run["path"] = ".github/workflows/untrusted.yml"
    elif failure == "foreign":
        github.run["head_repository"]["full_name"] = "attacker/repo"
    else:
        github.run["status"] = "completed"
    with pytest.raises(AdmissionDenied):
        capture_plan_artifact(github, 11, plan, "migration-apply", commit=COMMIT, session=SESSION)


def test_preparation_uses_actual_principal_metadata_and_configuration(monkeypatch):
    import time

    from lifecycle_fakes import CONFIG, OID

    from tirodhan.deployment import release_capture
    from tirodhan.deployment.deployment_admission import digest

    config = copy.deepcopy(CONFIG)
    for name in ("CI", "GITHUB_ACTIONS", "RUNNER_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)

    class Runner:
        def run(self, *args):
            if "status" in args:
                return ""
            return "d" * 40 if "HEAD^{tree}" in args else COMMIT

        def json(self, *args):
            if "account" in args:
                return {"tenantId": config["terraform"]["tenant_id"]}
            assert args[:4] == ("az", "ad", "sp", "show")
            assert config["deployment_client_id"] in args
            return {"id": OID, "appId": config["deployment_client_id"]}

    monkeypatch.setattr(release_capture, "contract_digest", lambda: CODE)
    request = release_capture.prepare(
        "release",
        session=SESSION,
        expires_at=int(time.time()) + 600,
        review_pr=123,
        github=ReviewGithub(),
        runner=Runner(),
        config=config,
    )
    assert request["principal_id"] == OID
    assert request["configuration_hash"] == digest(config)
    assert request["operations"] == sorted(release_capture.expected_bindings("release"))


def test_preparation_blocks_runner_before_any_external_read(monkeypatch):
    from tirodhan.deployment import release_capture

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(AdmissionDenied, match="TRUSTED_OPERATOR_HOST"):
        release_capture.prepare(
            "release",
            session=SESSION,
            expires_at=0,
            review_pr=123,
            github=None,
            runner=None,
            config={},
        )


def test_artifact_stream_exceeds_json_limit_without_leaking_auth_on_redirect(monkeypatch):
    import httpx

    from tirodhan.deployment import release_capture

    payload = b"synthetic-artifact" * 80000
    observed = []

    def respond(request):
        observed.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://artifacts.example/archive"})
        return httpx.Response(200, content=payload)

    original = httpx.Client
    monkeypatch.setattr(release_capture.Runner, "run", lambda *a, **k: "synthetic-test-token")
    monkeypatch.setattr(
        release_capture.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    assert release_capture.ReadOnlyGithub().download_artifact(11) == payload
    assert observed == [
        ("api.github.com", "Bearer synthetic-test-token"),
        ("artifacts.example", None),
    ]


@pytest.mark.parametrize("case", ["oversized", "streamed", "insecure", "failure"])
def test_artifact_download_is_bounded_and_sanitized(monkeypatch, case):
    import httpx

    from tirodhan.deployment import release_capture

    def respond(request):
        if case == "insecure":
            return httpx.Response(302, headers={"location": "http://untrusted.example/archive"})
        if case == "failure":
            raise httpx.ConnectError("must-not-print-token-or-signed-url", request=request)
        headers = {"content-length": "1000"} if case == "oversized" else {}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(b"x" * 100))

    original = httpx.Client
    monkeypatch.setattr(release_capture.Runner, "run", lambda *a, **k: "synthetic-test-token")
    monkeypatch.setattr(
        release_capture.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(AdmissionDenied) as error:
        release_capture.ReadOnlyGithub().download_artifact(11, limit=10)
    assert "synthetic-test-token" not in str(error.value)
    assert "must-not-print" not in str(error.value)
