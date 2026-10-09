"""Incremental review logic for --since-last-review flag.

Provides helpers to:
- Find the most recent AI review comment on a PR
- Read the commit a review comment recorded, and list the commits added after it
- Fetch the unified diff between two commit SHAs
"""

import re
import sys

import httpx

from review_pull_request.constants import REVIEW_SIGNATURE


_REVIEWED_COMMIT = re.compile(r"^> \*\*Reviewed commit:\*\* `([0-9a-f]{40})`$")


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


def _viewer_login(headers: dict) -> str | None:
    """The login of the account the token belongs to (the one that posts our reviews), or None."""
    try:
        resp = httpx.get("https://api.github.com/user", headers=headers, timeout=30.0)
        if resp.status_code != 200:
            return None
        login = resp.json().get("login")
        return login if isinstance(login, str) and login else None
    except Exception:
        return None


def find_last_review_comment(
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
) -> dict | None:
    """Find the most recent AI review comment on the PR.

    Fetches all issue-level comments (paginated) and returns the latest one that starts with
    REVIEW_SIGNATURE and was posted by the token's own account.

    Returns:
        Comment dict (with 'created_at', 'body', etc.) or None if not found.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "review-pull-request/1.0",
    }

    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    comments = _paginate_get(url, headers, params={"per_page": 100})
    if comments is None:
        return None

    # Only our own comments count: anyone who can comment could otherwise post a fake review
    # claiming the head was already reviewed. Unknown identity means no baseline (full review).
    me = _viewer_login(headers)
    if me is None:
        print("WARNING: could not determine which GitHub account this token belongs to (GET /user); "
              "no earlier review can be trusted as a baseline.", file=sys.stderr)
        return None
    # The marker must open the comment: a report that merely quotes it (e.g. in the overview
    # of a diff that mentions it) is not a review baseline.
    review_comments = [
        c for c in comments
        if (c.get("user") or {}).get("login") == me
        and c.get("body", "").lstrip().startswith(REVIEW_SIGNATURE)
    ]
    if not review_comments:
        return None

    # Return the most recent review comment
    return max(review_comments, key=lambda c: c["created_at"])


def reviewed_sha(comment_body: str) -> str | None:
    """Return the commit SHA a review comment records, or None if it records none.

    Only the header block (the heading and the quoted lines under it) is read, so a SHA
    quoted further down, e.g. in the overview, is never taken as the baseline.
    """
    for line in comment_body.lstrip().split("\n")[1:]:
        if line.strip() and not line.startswith(">"):
            return None
        match = _REVIEWED_COMMIT.match(line.strip())
        if match:
            return match.group(1)
    return None


def get_commits_between(
    owner: str,
    repo: str,
    base_sha: str,
    head_sha: str,
    token: str,
) -> list[dict] | None:
    """Return the commits after base_sha up to head_sha, oldest first.

    Returns [] when head_sha is base_sha, and None on an API error or when base_sha is
    not an ancestor of head_sha (force-push or rebase); the caller then reviews in full.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "review-pull-request/1.0",
    }
    url = f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"
    try:
        resp = httpx.get(url, headers=headers, timeout=30.0)
        if resp.status_code != 200:
            print(f"WARNING: GitHub compare API returned {resp.status_code}", file=sys.stderr)
            return None
        data = resp.json()
    except Exception as e:
        print(f"WARNING: failed to compare commits: {e}", file=sys.stderr)
        return None

    status = data.get("status")
    if status == "identical":
        return []
    if status != "ahead":
        print(f"WARNING: the last reviewed commit is not an ancestor of the head ({status}).",
              file=sys.stderr)
        return None
    return data.get("commits", [])


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
        "User-Agent": "review-pull-request/1.0",
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
