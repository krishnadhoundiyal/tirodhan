"""Validate CodeRabbit's completed no-comment format against its own check run."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from tirodhan.deployment.deployment_admission import AdmissionDenied

BOT_ID = 136622811
APP_ID = 347564
REPOSITORY = "krishnadhoundiyal/tirodhan"
STATUS = re.compile(
    r"(?m)^<!-- recent_review_status_start -->\s*"
    r"No actionable comments were generated in the recent review\.\s*"
    r"<!-- recent_review_status_end -->$"
)
COMMIT = re.compile(r'(?m)^<!-- change_assessment_commit:"([a-f0-9]{40})" -->$')


def comment_review_url(
    comment: dict[str, Any], check: dict[str, Any], source: str, number: int
) -> str:
    """A success check alone, a summary alone or a caller PASS flag cannot pass."""
    body = comment.get("body_excerpt", comment.get("body", ""))
    user = comment.get("user", {})
    app = check.get("app", {})
    if not isinstance(user, dict) or not isinstance(app, dict):
        raise AdmissionDenied("CODERABBIT_EVIDENCE_INVALID_OR_UNRESOLVED")
    expected_url = f"https://github.com/{REPOSITORY}/pull/{number}#issuecomment-{comment.get('id')}"
    if (
        not isinstance(body, str)
        or len(STATUS.findall(body.replace("\r\n", "\n"))) != 1
        or COMMIT.findall(body.replace("\r\n", "\n")) != [source]
        or "Currently processing new changes" in body
        or user.get("login") != "coderabbitai[bot]"
        or user.get("id") != BOT_ID
        or user.get("type") != "Bot"
        or type(comment.get("id")) is not int
        or not comment.get("updated_at")
        or comment.get("issue_url") != f"https://api.github.com/repos/{REPOSITORY}/issues/{number}"
        or comment.get("html_url") != expected_url
        or type(check.get("id")) is not int
        or check.get("head_sha") != source
        or check.get("name") != "CodeRabbit"
        or check.get("status") != "completed"
        or check.get("conclusion") != "success"
        or not check.get("completed_at")
        or app.get("id") != APP_ID
        or app.get("slug") != "coderabbitai"
    ):
        raise AdmissionDenied("CODERABBIT_EVIDENCE_INVALID_OR_UNRESOLVED")
    return expected_url


def projected_comment(
    comment: dict[str, Any], check: dict[str, Any], source: str, number: int
) -> dict[str, Any]:
    """Retain actual evidence excerpts and a digest, without scoped share URLs."""
    comment_review_url(comment, check, source, number)
    body = comment["body"].replace("\r\n", "\n")
    status = STATUS.search(body)
    assessment = COMMIT.search(body)
    if status is None or assessment is None:
        raise AdmissionDenied("CODERABBIT_EVIDENCE_INVALID_OR_UNRESOLVED")
    excerpt = status.group(0) + "\n" + assessment.group(0)
    return {
        **{key: comment[key] for key in ("id", "user", "updated_at", "issue_url", "html_url")},
        "body_excerpt": excerpt,
        "source_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
