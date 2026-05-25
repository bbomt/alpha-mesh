"""GitHub API client for PR review agent."""

import json
import re
import subprocess
import sys
from typing import Optional


class GitHubClient:
    """Client for GitHub REST API."""

    def __init__(self, token: Optional[str] = None):
        self.token = token or self._get_token()
        if not self.token:
            print(
                "WARNING: GitHub token not found. Some features may not work.",
                file=sys.stderr,
            )
            print(
                "  To fix: install 'gh' CLI (sudo dnf install gh) and run 'gh auth login', ",
                file=sys.stderr,
            )
            print(
                "  Or set GITHUB_TOKEN environment variable.",
                file=sys.stderr,
            )
        self.api_base = "https://api.github.com"

    def _command_exists(self, cmd: str) -> bool:
        """Check if a command exists on the system."""
        try:
            subprocess.run(["which", cmd], capture_output=True, check=True)
            return True
        except Exception:
            return False

    def _check_gh_installed(self) -> bool:
        """Check if gh CLI is installed and warn if not."""
        if not self._command_exists("gh"):
            print(
                "WARNING: 'gh' CLI is not installed.",
                file=sys.stderr,
            )
            print(
                "  Install on Amazon Linux 2023:",
                file=sys.stderr,
            )
            print(
                "    sudo dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo",
                file=sys.stderr,
            )
            print(
                "    sudo dnf install gh -y",
                file=sys.stderr,
            )
            print(
                "  Or continue using GITHUB_TOKEN environment variable.",
                file=sys.stderr,
            )
            return False
        return True

    def _get_token(self) -> Optional[str]:
        """Get GitHub token from gh CLI, git credential helper, or environment."""
        import os

        # Check if gh is installed
        gh_installed = self._check_gh_installed()

        # Try gh CLI (if installed)
        if gh_installed:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()

        # Fall back to git credential helper
        try:
            result = subprocess.run(
                ["git", "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith("password="):
                        token = line.split("=", 1)[1].strip()
                        if token:
                            return token
        except Exception:
            pass

        # Fall back to environment variable
        return os.environ.get("GITHUB_TOKEN")

    def _request(self, method: str, url: str, **kwargs) -> dict:
        """Make an HTTP request to GitHub API."""
        import requests

        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        headers.update(kwargs.pop("headers", {}))

        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            **kwargs,
        )

        if response.status_code == 403:
            if "rate limit" in response.text.lower():
                print("ERROR: GitHub API rate limit exceeded.", file=sys.stderr)
                sys.exit(2)
            print(f"ERROR: GitHub API returned 403: {response.text}", file=sys.stderr)
            sys.exit(2)

        if response.status_code >= 400:
            print(
                f"ERROR: GitHub API returned {response.status_code}: {response.text}",
                file=sys.stderr,
            )
            sys.exit(2)

        return response

    def get_pr(self, owner: str, repo: str, pr_number: int) -> dict:
        """Return PR metadata: title, body, head branch, base branch, state."""
        url = f"{self.api_base}/repos/{owner}/{repo}/pulls/{pr_number}"
        response = self._request("GET", url)
        return response.json()

    def get_pr_comments(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """Return all issue-level comments on the PR."""
        url = f"{self.api_base}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        response = self._request("GET", url)
        return response.json()

    def get_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Return the unified diff as a string."""
        url = f"{self.api_base}/repos/{owner}/{repo}/pulls/{pr_number}"
        headers = {"Accept": "application/vnd.github.v3.diff"}
        response = self._request("GET", url, headers=headers)
        return response.text

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        """Return list of changed files with filename, status, patch."""
        url = f"{self.api_base}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        response = self._request("GET", url)
        return response.json()

    def get_commit_sha(self, owner: str, repo: str, pr_number: int) -> str:
        """Get the head commit SHA for the PR."""
        pr = self.get_pr(owner, repo, pr_number)
        return pr.get("head", {}).get("sha", "")

    def create_pr_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        commit_sha: str,
        body: str,
        comments: list[dict],
    ) -> None:
        """Create a GitHub review with summary and inline comments."""
        url = f"{self.api_base}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        payload = {
            "commit_id": commit_sha,
            "body": body,
            "event": "COMMENT",
            "comments": comments,
        }
        self._request("POST", url, json=payload)

    def post_pr_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post a top-level markdown comment on the PR (legacy - use create_pr_review instead)."""
        url = f"{self.api_base}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        payload = {"body": body}
        self._request("POST", url, json=payload)


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Parse a GitHub PR URL and return (owner, repo, pr_number).

    Args:
        pr_url: Full GitHub PR URL, e.g. https://github.com/org/repo/pull/123

    Returns:
        Tuple of (owner, repo, pr_number)

    Raises:
        ValueError: If URL doesn't match expected pattern
    """
    pattern = r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    match = re.match(pattern, pr_url)
    if not match:
        raise ValueError(
            f"Invalid PR URL format. Expected: https://github.com/owner/repo/pull/N"
        )
    owner, repo, pr_number = match.groups()
    return owner, repo, int(pr_number)
