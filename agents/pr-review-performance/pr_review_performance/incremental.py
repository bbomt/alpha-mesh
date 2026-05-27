"""Incremental review logic for --since-last-review flag.

Provides helpers to:
- Find the most recent AI review comment on a PR
- List commits on the PR branch added after a given timestamp
- Fetch the unified diff between two commit SHAs
"""

import sys
from datetime import datetime, timezone

import httpx

from pr_review_performance.constants import REVIEW_SIGNATURE


def _parse_timestamp(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string to a timezone-aware datetime.

    Handles both 'Z' suffix and '+00:00' offset forms.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def find_last_review_comment(
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
) -> dict | None:
    """Find the most recent AI review comment on the PR.

    Fetches all issue-level comments and returns the one whose body contains
    REVIEW_SIGNATURE with the latest created_at timestamp.

    Returns:
        Comment dict (with 'created_at', 'body', etc.) or None if not found.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "pr-review-performance/1.0",
    }

    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"

    try:
        resp = httpx.get(url, headers=headers, timeout=30.0, params={"per_page": 100})
        if resp.status_code != 200:
            print(
                f"WARNING: GitHub API returned {resp.status_code} fetching comments",
                file=sys.stderr,
            )
            return None
        comments = resp.json()
    except Exception as e:
        print(f"WARNING: failed to fetch PR comments: {e}", file=sys.stderr)
        return None

    review_comments = [c for c in comments if REVIEW_SIGNATURE in c.get("body", "")]
    if not review_comments:
        return None

    # Return the most recent review comment
    return max(review_comments, key=lambda c: c["created_at"])


def get_commits_since(
    owner: str,
    repo: str,
    pr_number: int,
    since_timestamp: str,
    token: str,
) -> list[dict]:
    """Return PR commits whose committer date is strictly after since_timestamp.

    Args:
        since_timestamp: ISO 8601 string (e.g. "2024-01-15T10:30:00Z")

    Returns:
        List of commit dicts in chronological order (oldest first).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "pr-review-performance/1.0",
    }

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits"

    try:
        resp = httpx.get(url, headers=headers, timeout=30.0, params={"per_page": 100})
        if resp.status_code != 200:
            print(
                f"WARNING: GitHub API returned {resp.status_code} fetching commits",
                file=sys.stderr,
            )
            return []
        commits = resp.json()
    except Exception as e:
        print(f"WARNING: failed to fetch PR commits: {e}", file=sys.stderr)
        return []

    cutoff = _parse_timestamp(since_timestamp)

    new_commits = []
    for commit in commits:
        # Prefer committer date, fall back to author date
        commit_date_str = (
            commit.get("commit", {}).get("committer", {}).get("date", "")
            or commit.get("commit", {}).get("author", {}).get("date", "")
        )
        if not commit_date_str:
            continue
        if _parse_timestamp(commit_date_str) > cutoff:
            new_commits.append(commit)

    # Return in chronological order (oldest first)
    return sorted(
        new_commits,
        key=lambda c: (
            c.get("commit", {}).get("committer", {}).get("date", "")
            or c.get("commit", {}).get("author", {}).get("date", "")
        ),
    )


def build_incremental_diff(
    owner: str,
    repo: str,
    base_sha: str,
    head_sha: str,
    token: str,
) -> str:
    """Fetch the unified diff between base_sha and head_sha via GitHub compare API.

    Uses three-dot compare (base...head) which resolves from the common ancestor.
    For a linear commit history this is identical to two-dot.

    Returns:
        Diff as a string, or empty string on failure.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
        "User-Agent": "pr-review-performance/1.0",
    }

    url = f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"

    try:
        resp = httpx.get(url, headers=headers, timeout=60.0)
        if resp.status_code != 200:
            print(
                f"WARNING: GitHub compare API returned {resp.status_code}",
                file=sys.stderr,
            )
            return ""
        return resp.text
    except Exception as e:
        print(f"WARNING: failed to fetch incremental diff: {e}", file=sys.stderr)
        return ""
