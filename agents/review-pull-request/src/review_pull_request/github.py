"""Post a comment to a GitHub PR via REST API using httpx."""

import os
import subprocess

import httpx

from review_pull_request.formatter import FINDINGS_END
from review_pull_request.git import parse_pr_url


def _get_github_token() -> str | None:
    """Get GitHub token: gh auth token > GITHUB_TOKEN env var."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # gh not installed
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return os.environ.get("GITHUB_TOKEN") or None


MAX_COMMENT_CHARS = 65_000  # GitHub rejects comment bodies over 65,536 characters
TRUNCATION_NOTE = "\n\n… _report truncated to fit GitHub's comment size limit_"


def _truncate(body: str) -> str:
    """Cut the findings, not the coverage disclosures that follow them (FINDINGS_END)."""
    cut = body.rfind(FINDINGS_END)
    tail = body[cut:] if cut != -1 else ""
    room = MAX_COMMENT_CHARS - len(TRUNCATION_NOTE) - len(tail)
    if cut == -1 or room <= 0:  # no marker, or a tail too long to keep whole
        return body[: MAX_COMMENT_CHARS - len(TRUNCATION_NOTE)] + TRUNCATION_NOTE
    return body[:room] + TRUNCATION_NOTE + tail


def post_comment(body: str, pr_url: str, config) -> bool:
    """Post body as a comment on the GitHub PR at pr_url.

    Never raises. Returns True if the comment was posted, False otherwise.
    """
    github_token = _get_github_token()
    if not github_token:
        print("WARNING: GitHub token not available ( gh auth token | GITHUB_TOKEN ), skipping PR comment.", flush=True)
        return False

    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except Exception as e:
        print(f"WARNING: could not parse PR URL '{pr_url}': {e}", flush=True)
        return False

    if len(body) > MAX_COMMENT_CHARS:
        print(f"WARNING: report is {len(body)} characters; truncating to fit GitHub's limit.", flush=True)
        body = _truncate(body)

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "review-pull-request/1.0",
        "Content-Type": "application/json",
    }

    api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"

    try:
        response = httpx.post(
            api_url,
            json={"body": body},
            headers=headers,
            timeout=30.0,
        )
        if response.status_code in (200, 201):
            try:
                url = response.json().get("html_url", api_url)
            except Exception:  # the comment is posted; a bad body must not flip that to a failure
                url = api_url
            print(f"PR comment posted: {url}", flush=True)
            return True
        else:
            print(
                f"WARNING: GitHub API returned {response.status_code}: "
                f"{response.text[:200]}",
                flush=True,
            )
    except Exception as e:
        print(f"WARNING: failed to post PR comment: {e}", flush=True)
    return False
