"""Post a comment to a GitHub PR via REST API using httpx."""

import os
import subprocess

import httpx

from review_pull_request.git import parse_pr_url


def _get_github_token() -> str | None:
    """Get GitHub token: gh auth token > GITHUB_TOKEN env var."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):  # gh not installed
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return os.environ.get("GITHUB_TOKEN") or None


def post_comment(body: str, pr_url: str, config) -> None:
    """Post body as a comment on the GitHub PR at pr_url.

    Silently continues on failure (never raises).
    """
    github_token = _get_github_token()
    if not github_token:
        print("WARNING: GitHub token not available ( gh auth token | GITHUB_TOKEN ), skipping PR comment.", flush=True)
        return

    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except Exception as e:
        print(f"WARNING: could not parse PR URL '{pr_url}': {e}", flush=True)
        return

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
            data = response.json()
            print(f"PR comment posted: {data.get('html_url', api_url)}", flush=True)
        else:
            print(
                f"WARNING: GitHub API returned {response.status_code}: "
                f"{response.text[:200]}",
                flush=True,
            )
    except Exception as e:
        print(f"WARNING: failed to post PR comment: {e}", flush=True)
