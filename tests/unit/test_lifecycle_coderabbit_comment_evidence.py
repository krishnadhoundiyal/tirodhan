"""Completed no-comment reviews retain real bot/App/head/CI provenance."""

import copy
import json

import pytest
from test_lifecycle_release_capture import ReviewGithub
from test_lifecycle_release_setup import CODE, COMMIT, SOURCE

from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.release_capture import collect_review
from tirodhan.deployment.release_setup import validate_evidence


class CommentGithub(ReviewGithub):
    def __init__(self):
        super().__init__()
        self.formal = []
        self.comments = [
            {
                "id": 10,
                "user": {"id": 136622811, "login": "coderabbitai[bot]", "type": "Bot"},
                "updated_at": "2026-11-04T00:00:00Z",
                "issue_url": "https://api.github.com/repos/krishnadhoundiyal/tirodhan/issues/123",
                "html_url": "https://github.com/krishnadhoundiyal/tirodhan/pull/123#issuecomment-10",
                "body": (
                    "<!-- review_stack_entry_start -->\nhttps://example.invalid/?scope=do-not-retain\n"
                    "<!-- recent_review_status_start -->\n\n"
                    "No actionable comments were generated in the recent review.\n\n"
                    "<!-- recent_review_status_end -->\n"
                    f'<!-- change_assessment_commit:"{SOURCE}" -->'
                ),
            }
        ]
        self.checks = [
            {
                "id": 20,
                "name": "CodeRabbit",
                "head_sha": SOURCE,
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-11-04T00:00:01Z",
                "app": {"id": 347564, "slug": "coderabbitai"},
                "output": {"text": "https://example.invalid/?scope=do-not-retain"},
            }
        ]

    def get(self, path):
        if "/reviews?" in path:
            return self.formal
        if path.startswith("issues/"):
            return self.comments
        if path.startswith("commits/") and "/check-runs?" in path:
            return {"total_count": len(self.checks), "check_runs": self.checks}
        return super().get(path)


def test_completed_comment_and_own_app_check_are_a_review_not_a_pass_flag():
    evidence = collect_review(CommentGithub(), 123, COMMIT, CODE)
    assert evidence["review"] is None
    validated = validate_evidence(evidence, COMMIT)
    assert validated["coderabbit_review"].endswith("#issuecomment-10")
    assert "do-not-retain" not in json.dumps(evidence)
    assert "source_body_sha256" in evidence["review_comment"]


@pytest.mark.parametrize(
    "failure",
    [
        "wrong-app",
        "wrong-bot",
        "human",
        "wrong-pr",
        "wrong-head",
        "wrong-assessment",
        "pending",
        "failed",
        "processing",
        "ambiguous",
        "unresolved",
        "unmerged",
        "missing-ci",
    ],
)
def test_incomplete_spoofed_or_unbound_receipts_fail_closed(failure):
    github = CommentGithub()
    comment, check = github.comments[0], github.checks[0]
    if failure == "wrong-app":
        check["app"]["id"] = 15368  # A GitHub Actions check named CodeRabbit is insufficient.
    elif failure == "wrong-bot":
        comment["user"]["id"] = 1
    elif failure == "human":
        comment["user"]["type"] = "User"
    elif failure == "wrong-pr":
        comment["issue_url"] = comment["issue_url"].replace("123", "999")
    elif failure == "wrong-head":
        check["head_sha"] = COMMIT
    elif failure == "wrong-assessment":
        comment["body"] = comment["body"].replace(SOURCE, COMMIT)
    elif failure == "pending":
        check["status"] = "in_progress"
    elif failure == "failed":
        check["conclusion"] = "failure"
    elif failure == "processing":
        comment["body"] += "\nCurrently processing new changes"
    elif failure == "ambiguous":
        comment["body"] += f'\n<!-- change_assessment_commit:"{SOURCE}" -->'
    elif failure == "unresolved":
        github.evidence["unresolved_threads"] = [{"isResolved": False}]
    elif failure == "unmerged":
        github.evidence["pull"]["merged"] = False
    else:
        github.evidence["workflow_runs"] = []
    with pytest.raises(AdmissionDenied):
        collect_review(github, 123, COMMIT, CODE)


def test_older_success_does_not_hide_latest_failed_app_check():
    github = CommentGithub()
    github.checks.append(copy.deepcopy(github.checks[0]) | {"id": 21, "conclusion": "failure"})
    with pytest.raises(AdmissionDenied):
        collect_review(github, 123, COMMIT, CODE)


def test_formal_changes_requested_cannot_fall_back_to_a_clean_summary():
    github = CommentGithub()
    github.formal = [github.evidence["review"] | {"state": "CHANGES_REQUESTED"}]
    with pytest.raises(AdmissionDenied):
        collect_review(github, 123, COMMIT, CODE)


def test_signer_rechecks_receipt_instead_of_trusting_collector_pass():
    evidence = collect_review(CommentGithub(), 123, COMMIT, CODE)
    evidence["review_check_run"]["app"]["id"] = 15368
    evidence["review_passed"] = True
    with pytest.raises(AdmissionDenied):
        validate_evidence(evidence, COMMIT)


def test_comment_history_limit_cannot_omit_a_newer_review():
    github = CommentGithub()
    github.comments *= 100
    with pytest.raises(AdmissionDenied, match="REVIEW_HISTORY_EXCEEDS_SAFE_CAPTURE_BOUND"):
        collect_review(github, 123, COMMIT, CODE)
