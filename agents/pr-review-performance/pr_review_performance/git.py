"""Git operations for pr-review-performance.

get_diff: run git diff against origin/base_branch
get_pr_description: fetch PR body via GitHub REST API
detect_repo_slug: parse owner/repo from git remote
"""

import subprocess
import re
from urllib.parse import urlparse

import httpx


def get_diff(base_branch: str, repo_root: str) -> str:
    """Run git diff origin/<base_branch> and return unified diff text."""
    result = subprocess.run(
        ["git", "diff", f"origin/{base_branch}"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git diff failed: {result.stderr}"
        )
    return result.stdout


def get_changed_files(base_branch: str, repo_root: str) -> list[str]:
    """Return list of file paths changed vs origin/<base_branch>."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"origin/{base_branch}"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Parse a GitHub PR URL into (owner, repo, pr_number)."""
    parsed = urlparse(pr_url)
    path = parsed.path.strip("/")
    parts = path.split("/")
    if len(parts) < 4 or parts[-2] != "pull":
        raise ValueError(f"Invalid PR URL: {pr_url}")
    owner = parts[0]
    repo = parts[1]
    pr_number = int(parts[3])
    return owner, repo, pr_number


def get_pr_description(pr_url: str, token: str) -> str:
    """Fetch PR body from GitHub API. Returns empty string on failure."""
    if not token or not pr_url:
        return ""

    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except Exception:
        return ""

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "pr-review-performance/1.0",
    }

    try:
        response = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
            timeout=30.0,
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("body", "") or ""
        return ""
    except Exception:
        return ""


def detect_repo_slug(repo_root: str = ".") -> str:
    """Parse owner/repo slug from git remote get-url origin."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        return ""

    url = result.stdout.strip()

    # SSH: git@github.com:owner/repo.git
    ssh_match = re.match(r"git@github\.com:([^/]+)/(.+?)(?:\.git)?$", url)
    if ssh_match:
        return f"{ssh_match.group(1)}/{ssh_match.group(2)}"

    # HTTPS: https://github.com/owner/repo.git
    https_match = re.match(r"https://github\.com/([^/]+)/(.+?)(?:\.git)?$", url)
    if https_match:
        return f"{https_match.group(1)}/{https_match.group(2)}"

    return ""
