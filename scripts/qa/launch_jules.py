#!/usr/bin/env python3
"""Dispatch independent routing QA to the Jules API; no repository write permission needed."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://jules.googleapis.com/v1alpha"


def request(api_key: str, path: str, payload: dict | None = None) -> dict:
    headers = {"x-goog-api-key": api_key, "Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    method = "POST" if body is not None else "GET"
    req = Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=40) as response:
            return json.load(response)
    except HTTPError as error:
        # Never print the API key or raw provider response body.
        raise RuntimeError(f"Jules API {method} {path.split('?')[0]} returned HTTP {error.code}") from None
    except URLError as error:
        raise RuntimeError(f"Jules API connection failed ({type(error.reason).__name__})") from None


def find_source(api_key: str, repository: str) -> str:
    if repository.count("/") != 1:
        raise ValueError("Expected owner/repository in JULES_GITHUB_REPO")
    owner, name = (part.casefold() for part in repository.split("/"))
    page_token = ""
    for _ in range(50):
        query = urlencode({"pageSize": 100, **({"pageToken": page_token} if page_token else {})})
        response = request(api_key, "/sources?" + query)
        for source in response.get("sources", []):
            github = source.get("githubRepo", {})
            if (
                str(github.get("owner", "")).casefold() == owner
                and str(github.get("repo", "")).casefold() == name
            ):
                return source["name"]
        page_token = response.get("nextPageToken", "")
        if not page_token:
            break
    raise RuntimeError(
        f"Repository {repository} is not connected to Jules. Connect it at jules.google.com first."
    )


def main() -> None:
    key = os.environ.get("JULES_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Missing JULES_API_KEY GitHub Actions secret. Add it in repository Settings > "
            "Secrets and variables > Actions, after generating the key in Jules Settings."
        )
    repository = os.environ["JULES_GITHUB_REPO"]
    sha = os.environ["JULES_TARGET_SHA"]
    event = os.environ.get("JULES_EVENT_NAME", "workflow_dispatch")
    source = find_source(key, repository)
    mandate = Path("docs/qa/routing/QA_MANDATE.md").read_text(encoding="utf-8")

    description = (
        f"This is an AUTOMATED independent routing QA run for {repository}, "
        f"triggered by {event} at GitHub commit {sha}.\n"
        "Start from main, report the actual implementation SHA, and identify a mismatch "
        "if main has advanced since the trigger. Read the existing root AGENTS.md and "
        "all relevant architecture/QA documents.\n"
        "Implement and EXECUTE the following standing QA mandate. Do not merely propose "
        "a plan or summarize old tests. Own tests, fixtures, QA reports and QA-only PRs; "
        "do not edit production source or merge/deploy.\n\n"
        + mandate
    )
    session = request(
        key,
        "/sessions",
        {
            "prompt": description,
            "title": f"Tirodhan routing QA [{event}] {sha[:12]}",
            "sourceContext": {
                "source": source,
                "githubRepoContext": {"startingBranch": "main"},
            },
            "requirePlanApproval": False,
            "automationMode": "AUTO_CREATE_PR",
        },
    )

    session_name = session.get("name", "unknown")
    session_url = session.get("url", "")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    text = [
        "## Jules routing QA dispatched",
        "",
        f"- GitHub event: `{event}`",
        f"- Triggering commit: `{sha}`",
        f"- Jules session: `{session_name}`",
        "- The GitHub workflow confirms dispatch **only**; Jules test results arrive later.",
        "- Any changes from Jules require human review of its QA PR; no automatic merge.",
    ]
    if session_url.startswith("https://jules.google.com/"):
        text.append(f"- [Open Jules QA session]({session_url})")
    message = "\n".join(text) + "\n"
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(message)
    print(f"Dispatched Jules routing QA: {session_name}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        print(f"Jules QA dispatch failed: {error}", file=sys.stderr)
        sys.exit(1)
