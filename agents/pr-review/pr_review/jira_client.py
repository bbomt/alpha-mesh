"""JIRA client for PR review agent - extracts ticket IDs and verifies MCP auth."""

import re
import subprocess
import sys
from typing import Optional


def extract_jira_ids(pr_title: str, branch_name: str, pr_body: str) -> list[str]:
    """Scan all three strings for patterns matching JIRA ticket IDs.

    Pattern: one or more uppercase letters, a hyphen, one or more digits.
    Example matches: ABC-123, MYPROJECT-4567, FOO-1

    Returns deduplicated list preserving order of first appearance.
    """
    pattern = r"[A-Z][A-Z0-9]+-\d+"
    seen = set()
    result = []

    for text in [pr_title, branch_name, pr_body or ""]:
        for match in re.finditer(pattern, text):
            jira_id = match.group(0)
            if jira_id not in seen:
                seen.add(jira_id)
                result.append(jira_id)

    return result


def verify_atlassian_auth(repo_path: str, verbose: bool = False) -> bool:
    """Verify that Atlassian MCP plugin is authenticated in Claude Code.

    Runs a minimal Claude Code command that attempts to list Atlassian MCP tools.
    If the response indicates auth is missing or the plugin is not connected, return False.

    Args:
        repo_path: Path to the repository where Claude Code should run
        verbose: If True, print detailed output

    Returns:
        True if authenticated, False otherwise
    """
    # Run claude with --version or a simple check
    # We'll try to see if claude is available and has the Atlassian plugin
    try:
        result = subprocess.run(
            ["claude", "--version"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            if verbose:
                print(f"Claude Code not found or error: {result.stderr}", file=sys.stderr)
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        if verbose:
            print(f"Claude Code not available: {e}", file=sys.stderr)
        return False

    # Try to check if Atlassian plugin is available by running a simple command
    # We use --print to run headless and check for Atlassian tools
    try:
        result = subprocess.run(
            [
                "claude",
                "--print",
                "--no-input",
                "-p",
                "List available MCP tools. If you have an Atlassian plugin connected, "
                "list the Atlassian tools available. If not, say 'Atlassian plugin not found'.",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = result.stdout + result.stderr
        if verbose:
            print(f"Auth check output: {output}", file=sys.stderr)

        # Check if Atlassian tools are mentioned in output
        if "atlassian" in output.lower() and (
            "getjiraissue" in output.lower()
            or "search" in output.lower()
            or "mcp" in output.lower()
        ):
            return True

        # If we get specific error messages about auth
        if "auth" in output.lower() or "login" in output.lower():
            return False

        # If "Atlassian plugin not found" or similar
        if "not found" in output.lower() or "not connected" in output.lower():
            return False

        # Be optimistic - if Claude Code ran without auth errors, assume it's OK
        # The actual JIRA fetch will fail later with a clearer error if not auth'd
        return True

    except subprocess.TimeoutExpired:
        if verbose:
            print("Timeout checking Atlassian auth", file=sys.stderr)
        return False
    except Exception as e:
        if verbose:
            print(f"Error checking Atlassian auth: {e}", file=sys.stderr)
        return False
