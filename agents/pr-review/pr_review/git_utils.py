"""Git utilities for PR review agent."""

import subprocess
import sys
from pathlib import Path


class GitError(Exception):
    """Exception raised for git-related errors."""
    pass


def get_current_branch(repo_path: str) -> str:
    """Return current branch name."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"Failed to get current branch: {result.stderr}")
    return result.stdout.strip()


def check_clean_state(repo_path: str) -> bool:
    """Return True if working tree is clean (no uncommitted changes)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"Failed to check git status: {result.stderr}")
    return result.stdout.strip() == ""


def checkout_branch(repo_path: str, branch_name: str) -> None:
    """Check out the given branch. Raises GitError if there are uncommitted changes."""
    if not check_clean_state(repo_path):
        print(
            "ERROR: Working tree is not clean. Please stash or commit your local changes before running pr-review.",
            file=sys.stderr,
        )
        raise GitError("Working tree is not clean")

    result = subprocess.run(
        ["git", "checkout", branch_name],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"Failed to checkout branch '{branch_name}': {result.stderr}")


def pull_latest(repo_path: str, branch_name: str) -> None:
    """Pull latest changes for the branch before checking out."""
    result = subprocess.run(
        ["git", "pull", "origin", branch_name],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"Failed to pull latest changes: {result.stderr}")


def commit_and_push(repo_path: str, branch_name: str, message: str) -> None:
    """Stage all changes, create a commit, and push to origin/{branch_name}."""
    # Stage all changes
    result = subprocess.run(
        ["git", "add", "-A"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"Failed to stage changes: {result.stderr}")

    # Create commit
    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"Failed to create commit: {result.stderr}")

    # Push to remote
    result = subprocess.run(
        ["git", "push", "origin", branch_name],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"Failed to push to origin/{branch_name}: {result.stderr}")
