"""Incremental review logic for --since-last-review flag.

Provides helpers to:
- Find the most recent AI review comment on a PR
- List commits on the PR branch added after a given timestamp
- Fetch the unified diff between two commit SHAs
"""

import re
import sys
from datetime import datetime

import httpx

from pr_review_performance.constants import REVIEW_SIGNATURE


def _parse_timestamp(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string to a timezone-aware datetime.

    Handles both 'Z' suffix and '+00:00' offset forms.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _next_page_url(link_header: str) -> str | None:
    """Extract the 'next' URL from a GitHub Link header, or None if absent."""
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
    return match.group(1) if match else None


def _paginate_get(url: str, headers: dict, params: dict | None = None) -> list | None:
    """Fetch all pages of a GitHub JSON list endpoint.

    Returns:
        Combined list from all pages, or None on any HTTP/network error.
    """
    items: list = []
    next_url: str | None = url
    request_params = dict(params or {})

    while next_url:
        try:
            resp = httpx.get(next_url, headers=headers, timeout=30.0, params=request_params)
        except Exception as e:
            print(f"WARNING: GitHub API request failed: {e}", file=sys.stderr)
            return None

        if resp.status_code != 200:
            print(
                f"WARNING: GitHub API returned {resp.status_code} for {next_url}",
                file=sys.stderr,
            )
            return None

        page_items = resp.json()
        if not isinstance(page_items, list):
            return None
        items.extend(page_items)

        # Follow pagination via Link header; clear params so the next URL is used as-is
        next_url = _next_page_url(resp.headers.get("Link", ""))
        request_params = {}

    return items


def find_last_review_comment(
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
) -> dict | None:
    """Find the most recent AI review comment on the PR.

    Fetches all issue-level comments (paginated) and returns the one whose body
    contains REVIEW_SIGNATURE with the latest created_at timestamp.

    Returns:
        Comment dict (with 'created_at', 'body', etc.) or None if not found.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "pr-review-performance/1.0",
    }

    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    comments = _paginate_get(url, headers, params={"per_page": 100})
    if comments is None:
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
) -> list[dict] | None:
    """Return PR commits whose committer date is strictly after since_timestamp.

    Args:
        since_timestamp: ISO 8601 string (e.g. "2024-01-15T10:30:00Z")

    Returns:
        List of commit dicts in chronological order (oldest first), or None on
        API/network error. An empty list means no new commits exist (not an error).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "pr-review-performance/1.0",
    }

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits"
    commits = _paginate_get(url, headers, params={"per_page": 100})
    if commits is None:
        return None

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
