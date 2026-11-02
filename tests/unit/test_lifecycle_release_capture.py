"""Authenticated collector boundaries: failed evidence and unrelated artifacts cannot pass."""

import copy
import io
import zipfile

import pytest
from test_lifecycle_release_setup import CODE, COMMIT, SESSION, SOURCE, review_evidence

from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.release_capture import capture_plan_artifact, collect_review


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

    def command(self, path):
        assert path == "repos/krishnadhoundiyal/tirodhan/actions/artifacts/11/zip"
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
