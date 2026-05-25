"""Post a comment to a GitHub PR via REST API using httpx."""

import os

import httpx

from pr_review_performance.git import parse_pr_url


def post_comment(body: str, pr_url: str, config) -> None:
    """Post body as a comment on the GitHub PR at pr_url.

    Silently continues on failure (never raises).
    """
    github_token = os.environ.get(config.github_token_env_var, "")
    if not github_token:
        print("WARNING: GitHub token not available, skipping PR comment.", flush=True)
        return

    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except Exception as e:
        print(f"WARNING: could not parse PR URL '{pr_url}': {e}", flush=True)
        return

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "pr-review-performance/1.0",
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
