"""CLI entry point for PR review agent."""

import argparse
import os
import sys
import traceback
from pathlib import Path

from pr_review.github_client import GitHubClient, parse_pr_url
from pr_review.jira_client import extract_jira_ids, verify_atlassian_auth
from pr_review.context_builder import build_review_prompt
from pr_review.claude_runner import run_claude_code_review, ClaudeRunnerError
from pr_review.reviewer import parse_review_output, ReviewParseError
from pr_review.pr_commenter import post_review
from pr_review.git_utils import (
    get_current_branch,
    check_clean_state,
    checkout_branch,
    pull_latest,
    commit_and_push,
    GitError,
)


import time

LOG_DIR = Path.home() / "environment" / "logs" / "ai" / "claude"
LOG_FILE = LOG_DIR / f"alpha-mesh-{time.strftime('%Y-%m-%d')}.log"


def setup_logging(verbose: bool = False) -> None:
    """Set up logging to file as per CLAUDE.md conventions."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # We'll just ensure the directory exists
    # Actual command logging is done via >> redirect in shell or print statements


def log_command(cmd: str, output: str = "", error: str = "", verbose: bool = False) -> None:
    """Log a command and its output to the log file."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"\n$ {cmd}\n")
            if output:
                f.write(f"OUTPUT:\n{output}\n")
            if error:
                f.write(f"ERROR:\n{error}\n")
    except Exception:
        pass  # Don't let logging failures break the tool

    if verbose:
        print(f"$ {cmd}", file=sys.stderr)
        if output:
            print(output, file=sys.stderr)
        if error:
            print(f"ERROR: {error}", file=sys.stderr)


def main() -> int:
    """Main entry point for pr-review."""
    parser = argparse.ArgumentParser(
        description="PR Review Agent - AI-powered code review via Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  pr-review https://github.com/org/repo/pull/123
  pr-review https://github.com/org/repo/pull/123 --auto-fix
  pr-review https://github.com/org/repo/pull/123 --dry-run --verbose
"""
    )
    parser.add_argument(
        "pr_url",
        help="Full GitHub PR URL, e.g. https://github.com/org/repo/pull/123",
    )
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        help="Run Claude Code with edit permissions; commit and push fixes to the PR branch",
    )
    parser.add_argument(
        "--repo-path",
        type=str,
        default=None,
        help="Local path to the repository (default: current working directory)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed logs to stderr",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run everything except posting the comment and pushing",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds for Claude Code (default: 300)",
    )

    args = parser.parse_args()
    verbose = args.verbose
    original_branch = None

    # ─── Step 1: Parse & validate PR URL ────────────────────────────────
    log_command(f"Parsing PR URL: {args.pr_url}", verbose=verbose)
    try:
        owner, repo, pr_number = parse_pr_url(args.pr_url)
        print(f"PR: {owner}/{repo}#{pr_number}", file=sys.stderr)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # ─── Step 2: Determine repo path ─────────────────────────────────────
    repo_path = args.repo_path or os.environ.get("OLDPWD", os.getcwd())
    if not os.path.isdir(repo_path):
        print(f"ERROR: Repository path not found: {repo_path}", file=sys.stderr)
        return 1

    print(f"Repo path: {repo_path}", file=sys.stderr)
    log_command(f"Repo path: {repo_path}", verbose=verbose)

    # ─── Step 3: Fetch from GitHub ────────────────────────────────────────
    print("Fetching PR data from GitHub...", file=sys.stderr)
    log_command("Initializing GitHub client", verbose=verbose)
    try:
        github = GitHubClient()
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        log_command(f"Fetching PR: {owner}/{repo}#{pr_number}", verbose=verbose)
        pr = github.get_pr(owner, repo, pr_number)
        print(f"PR Title: {pr.get('title', '')}", file=sys.stderr)

        log_command("Fetching PR diff", verbose=verbose)
        diff = github.get_pr_diff(owner, repo, pr_number)

        log_command("Fetching PR files", verbose=verbose)
        changed_files = github.get_pr_files(owner, repo, pr_number)

        print(f"Changed files: {len(changed_files)}", file=sys.stderr)
    except SystemExit as e:
        # GitHub client uses sys.exit() for fatal errors
        return e.code if isinstance(e.code, int) else 2
    except Exception as e:
        print(f"ERROR: Failed to fetch PR data: {e}", file=sys.stderr)
        if verbose:
            traceback.print_exc(file=sys.stderr)
        return 2

    # ─── Step 4: Extract JIRA IDs ───────────────────────────────────────
    pr_title = pr.get("title", "")
    pr_body = pr.get("body") or ""
    branch_name = pr.get("head", {}).get("ref", "")

    log_command(f"Extracting JIRA IDs from title and branch", verbose=verbose)
    jira_ids = extract_jira_ids(pr_title, branch_name, pr_body)
    if jira_ids:
        print(f"JIRA IDs found: {', '.join(jira_ids)}", file=sys.stderr)
    else:
        print("No JIRA IDs found in PR title, branch, or body.", file=sys.stderr)

    # ─── Step 5: Verify Atlassian MCP auth ──────────────────────────────
    if jira_ids:
        print("Verifying Atlassian MCP auth...", file=sys.stderr)
        log_command("Verifying Atlassian MCP auth", verbose=verbose)
        if not verify_atlassian_auth(repo_path, verbose=verbose):
            print("", file=sys.stderr)
            print("ERROR: Atlassian MCP plugin is not authenticated in Claude Code.", file=sys.stderr)
            print("Please run `claude` in your terminal, activate the Atlassian plugin,", file=sys.stderr)
            print("and complete the OAuth flow.", file=sys.stderr)
            return 3

    # ─── Step 6: Checkout PR branch ────────────────────────────────────
    print(f"Checking out PR branch: {branch_name}", file=sys.stderr)
    log_command(f"Checking out branch: {branch_name}", verbose=verbose)
    try:
        # Save current branch for later
        original_branch = get_current_branch(repo_path)
        checkout_branch(repo_path, branch_name)
        pull_latest(repo_path, branch_name)
    except GitError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 5
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 5

    # ─── Step 7: Build context prompt ───────────────────────────────────
    print("Building review prompt...", file=sys.stderr)
    log_command("Building context prompt", verbose=verbose)
    prompt = build_review_prompt(
        pr=pr,
        diff=diff,
        changed_files=changed_files,
        jira_ids=jira_ids,
        auto_fix=args.auto_fix,
        repo_path=repo_path,
    )

    if verbose:
        print(f"Prompt length: {len(prompt)} chars", file=sys.stderr)
        print(prompt[:500], file=sys.stderr)

    # ─── Step 8: Run Claude Code ────────────────────────────────────────
    print("Running Claude Code review...", file=sys.stderr)
    log_command("Running Claude Code", verbose=verbose)
    try:
        raw_output = run_claude_code_review(
            repo_path=repo_path,
            prompt=prompt,
            auto_fix=args.auto_fix,
            timeout=args.timeout,
            verbose=verbose,
        )
    except ClaudeRunnerError as e:
        print(f"ERROR: Claude Code failed: {e}", file=sys.stderr)
        # Try to restore original branch
        if original_branch:
            try:
                checkout_branch(repo_path, original_branch)
            except Exception:
                pass
        return 4

    # ─── Step 9: Parse review output ────────────────────────────────────
    print("Parsing review output...", file=sys.stderr)
    log_command("Parsing review output", verbose=verbose)
    try:
        review = parse_review_output(raw_output)
        print(f"Review verdict: {review.verdict}", file=sys.stderr)
        print(f"Inline comments: {len(review.inline_comments)}", file=sys.stderr)
    except ReviewParseError as e:
        print(f"ERROR: Failed to parse review: {e}", file=sys.stderr)
        if verbose:
            print(f"Raw output (first 1000 chars):", file=sys.stderr)
            print(raw_output[:1000] if raw_output else "(empty)", file=sys.stderr)
        if original_branch:
            try:
                checkout_branch(repo_path, original_branch)
            except Exception:
                pass
        return 4

    # ─── Step 10: Post to GitHub PR ────────────────────────────────────
    print("Posting review to GitHub PR...", file=sys.stderr)
    log_command(f"Posting review (dry_run={args.dry_run})", verbose=verbose)
    try:
        post_review(
            github=github,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            pr=pr,
            review=review,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            print("Review posted successfully!", file=sys.stderr)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    except Exception as e:
        print(f"ERROR: Failed to post review: {e}", file=sys.stderr)
        if verbose:
            traceback.print_exc(file=sys.stderr)
        return 2

    # ─── Step 11: Auto-fix mode ────────────────────────────────────────
    if args.auto_fix:
        print("Auto-fix mode: committing and pushing changes...", file=sys.stderr)
        log_command("Committing and pushing changes", verbose=verbose)
        try:
            if not check_clean_state(repo_path):
                commit_and_push(
                    repo_path=repo_path,
                    branch_name=branch_name,
                    message="chore: apply AI code review suggestions [pr-review]",
                )
                print("Changes committed and pushed!", file=sys.stderr)
            else:
                print("No changes to commit.", file=sys.stderr)
        except GitError as e:
            print(f"ERROR: Git operation failed: {e}", file=sys.stderr)
            return 5
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 5

    # ─── Restore original branch ─────────────────────────────────────────
    if original_branch:
        try:
            checkout_branch(repo_path, original_branch)
        except Exception:
            print(f"Warning: Could not restore original branch '{original_branch}'", file=sys.stderr)

    print("PR review complete!", file=sys.stderr)
    log_command("pr-review completed successfully", verbose=verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
