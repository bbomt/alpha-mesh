"""CLI entry point for code-review plugin agent (PMAT-188).

This uses the /code-review plugin headlessly with JIRA context,
letting it post its own comments to GitHub.
"""

import argparse
import os
import sys
import traceback
from pathlib import Path

from agent_runner import (
    AgentContext,
    JiraTicket,
    GitHubData,
    run_claude_headless,
    ClaudeInvocationError,
)
from agent_runner import JiraContextGatherer, GitHubContextGatherer
from pr_review.plugin_handler import CodeReviewPluginHandler
from pr_review.jira_client import extract_jira_ids, verify_atlassian_auth
from pr_review.git_utils import (
    get_current_branch,
    check_clean_state,
    checkout_branch,
    pull_latest,
    GitError,
)
from pr_review.github_client import parse_pr_url
from pr_review.pr_commenter import post_review
from pr_review.github_client import GitHubClient


def get_diff(owner: str, repo: str, pr_number: int) -> str:
    """Fetch the unified diff for a PR via GitHub API."""
    import subprocess
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_number}", "--jq", ".diff"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout if result.returncode == 0 else ""


import time

LOG_DIR = Path.home() / "environment" / "logs" / "ai" / "claude"
LOG_FILE = LOG_DIR / f"alpha-mesh-{time.strftime('%Y-%m-%d')}.log"


def log_command(cmd: str, output: str = "", error: str = "", verbose: bool = False) -> None:
    """Log a command and its output to the log file."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"\n$ {cmd}\n")
            if output:
                f.write(f"OUTPUT:\n{output}\n")
            if error:
                f.write(f"ERROR:\n{error}\n")
    except Exception:
        pass

    if verbose:
        print(f"$ {cmd}", file=sys.stderr)
        if output:
            print(output[:500] + "..." if len(output) > 500 else output, file=sys.stderr)
        if error:
            print(f"ERROR: {error}", file=sys.stderr)


def main() -> int:
    """Main entry point for plugin-based code review."""
    parser = argparse.ArgumentParser(
        description="PR Review Agent - AI-powered code review via /code-review plugin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  pr-review-plugin https://github.com/org/repo/pull/123
  pr-review-plugin https://github.com/org/repo/pull/123 --verbose
"""
    )
    parser.add_argument(
        "pr_url",
        help="Full GitHub PR URL, e.g. https://github.com/org/repo/pull/123",
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
        help="Run everything except posting the comment",
    )
    parser.add_argument(
        "--no-jira",
        action="store_true",
        help="Skip JIRA ticket lookup (skip Atlassian auth check and JIRA ticket retrieval)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=2400,
        help="Timeout in seconds for Claude Code (default: 2400)",
    )
    parser.add_argument(
        "--show-pr-comment",
        action="store_true",
        help="Print the posted PR comment to stdout after posting",
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
    repo_path = args.repo_path or os.environ.get("REPO_PATH") or os.getcwd()
    if not os.path.isdir(repo_path):
        print(f"ERROR: Repository path not found: {repo_path}", file=sys.stderr)
        return 1

    print(f"Repo path: {repo_path}", file=sys.stderr)
    log_command(f"Repo path: {repo_path}", verbose=verbose)

    # ─── Step 3: Fetch from GitHub ────────────────────────────────────────
    print("Fetching PR data from GitHub...", file=sys.stderr)

    try:
        import subprocess
        import json

        # Build gh repo spec
        gh_repo = f"{owner}/{repo}"

        # Get PR details
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", gh_repo, "--json", "title,body,author,headRefName,baseRefName,state"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"ERROR: Failed to fetch PR: {result.stderr}", file=sys.stderr)
            return 2

        pr_data = json.loads(result.stdout)
        pr_title = pr_data.get("title", "")
        pr_body = pr_data.get("body", "") or ""
        branch_name = pr_data.get("headRefName", "")
        author = pr_data.get("author", {}).get("login", "")
        base_branch = pr_data.get("baseRefName", "")
        state = pr_data.get("state", "")

        # Get head commit SHA via gh api (gh pr view --json doesn't expose head.sha)
        sha_result = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_number}", "--jq", ".head.sha"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        head_sha = sha_result.stdout.strip() if sha_result.returncode == 0 else ""

        print(f"PR Title: {pr_title}", file=sys.stderr)

    except Exception as e:
        print(f"ERROR: Failed to fetch PR data: {e}", file=sys.stderr)
        if verbose:
            traceback.print_exc(file=sys.stderr)
        return 2

    # ─── Step 4: Extract JIRA IDs ───────────────────────────────────────
    jira_ids = extract_jira_ids(pr_title, branch_name, pr_body)
    if jira_ids:
        print(f"JIRA IDs found: {', '.join(jira_ids)}", file=sys.stderr)
    else:
        print("No JIRA IDs found in PR title, branch, or body.", file=sys.stderr)

    # ─── Step 5: Verify Atlassian MCP auth ──────────────────────────────
    if jira_ids and not args.no_jira:
        print("Verifying Atlassian MCP auth...", file=sys.stderr)
        log_command("Verifying Atlassian MCP auth", verbose=verbose)
        if not verify_atlassian_auth(repo_path, verbose=verbose):
            print("", file=sys.stderr)
            print("ERROR: Atlassian MCP plugin is not authenticated in Claude Code.", file=sys.stderr)
            print("Please run `claude` in your terminal, activate the Atlassian plugin,", file=sys.stderr)
            print("and complete the OAuth flow.", file=sys.stderr)
            print("Or use --no-jira to skip JIRA ticket lookup.", file=sys.stderr)
            return 3
    elif args.no_jira:
        print("Skipping JIRA auth check (--no-jira)", file=sys.stderr)

    # ─── Step 6: Checkout PR branch ────────────────────────────────────
    print(f"Checking out PR branch: {branch_name}", file=sys.stderr)
    log_command(f"Checking out branch: {branch_name}", verbose=verbose)
    try:
        original_branch = get_current_branch(repo_path)
        checkout_branch(repo_path, branch_name)
        pull_latest(repo_path, branch_name)
    except GitError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 5
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 5

    # ─── Step 7: Build context ────────────────────────────────────────────
    print("Building agent context...", file=sys.stderr)

    # Create GitHub data
    github_data = GitHubData(
        pr_url=args.pr_url,
        pr_title=pr_title,
        pr_body=pr_body,
        author=author,
        head_branch=branch_name,
        base_branch=base_branch,
        state=state,
    )

    # Create agent context with JIRA IDs
    context = AgentContext(
        agent_name="code-review",
        target=args.pr_url,
        metadata={"jira_ids": jira_ids, "repo_path": repo_path},
        github_data=github_data,
    )

    # Gather JIRA context if not skipped
    if jira_ids and not args.no_jira:
        print("Gathering JIRA ticket context...", file=sys.stderr)
        jira_gatherer = JiraContextGatherer()
        context = jira_gatherer.gather(context)
        if context.jira_tickets:
            print(f"Fetched details for {len(context.jira_tickets)} JIRA tickets", file=sys.stderr)

    # ─── Step 8: Run /code-review plugin ────────────────────────────────
    print("Running /code-review plugin...", file=sys.stderr)
    log_command("Running /code-review plugin", verbose=verbose)

    handler = CodeReviewPluginHandler()

    if args.dry_run:
        prompt = handler.build_prompt(context)
        print("=" * 60, file=sys.stderr)
        print("DRY RUN - Would invoke /code-review with:", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(prompt, file=sys.stderr)
        print("=" * 60, file=sys.stderr)

        # Restore original branch
        if original_branch:
            try:
                checkout_branch(repo_path, original_branch)
            except Exception:
                pass

        print("PR review complete (dry run)!", file=sys.stderr)
        return 0

    try:
        result = handler.run(context, repo_path, timeout=args.timeout, verbose=verbose)

        print(f"Plugin completed.", file=sys.stderr)
        print(f"Issues found: {result.get('issues_found', 'unknown')}", file=sys.stderr)
        print(f"Issue count: {result.get('issue_count', 0)}", file=sys.stderr)

        # Fetch diff for inline comment placement (only needed when we have a structured review)
        diff = get_diff(owner, repo, pr_number) if result.get("review") else ""

        # Track what we post for verification
        posted_body = ""
        post_target = ""
        combined = ""

        # Post review to GitHub ourselves using the parsed Review object
        review = result.get("review")
        review_text = result.get("review_text", "")

        if review:
            # Post structured Review object
            print("Posting review to GitHub PR...", file=sys.stderr)
            github = GitHubClient()
            try:
                # Build pr dict with head.sha for post_review
                pr_with_sha = {**pr_data, "head": {"sha": head_sha}}
                combined = post_review(
                    github=github,
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    pr=pr_with_sha,
                    review=review,
                    dry_run=args.dry_run,
                    diff=diff,
                )
                if not args.dry_run:
                    print("Review posted successfully!", file=sys.stderr)
                    log_command("code-review plugin completed successfully", verbose=verbose)
                    post_target = f"repos/{owner}/{repo}/pulls/{pr_number}/reviews"
            except Exception as e:
                print(f"ERROR: Failed to post review: {e}", file=sys.stderr)
                if verbose:
                    traceback.print_exc(file=sys.stderr)
                return 2
        elif review_text:
            # No structured Review but has text findings — post raw markdown
            print("Posting text review to GitHub PR...", file=sys.stderr)
            github = GitHubClient()
            try:
                github.post_pr_comment(
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    body=review_text,
                )
                if not args.dry_run:
                    print("Review posted successfully!", file=sys.stderr)
                    log_command("code-review plugin completed successfully", verbose=verbose)
                    posted_body = review_text
                    post_target = f"repos/{owner}/{repo}/issues/{pr_number}/comments"
            except Exception as e:
                print(f"ERROR: Failed to post review: {e}", file=sys.stderr)
                if verbose:
                    traceback.print_exc(file=sys.stderr)
                return 2
        else:
            print("No review issues found, skipping posting.", file=sys.stderr)

        # Show full comment if --show-pr-comment
        if args.show_pr_comment and combined:
            print(combined)
        elif args.show_pr_comment and review_text:
            print(review_text)

        # Verify comment was posted and show it if requested
        if not args.dry_run and post_target:
            import subprocess
            verify_result = subprocess.run(
                ["gh", "api", post_target, "--jq", "last|.body"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if verify_result.returncode == 0 and verify_result.stdout.strip():
                print("\n" + "=" * 60, file=sys.stderr)
                print("VERIFIED: Comment posted to PR:", file=sys.stderr)
                print("=" * 60, file=sys.stderr)
                print(verify_result.stdout.strip()[:100], file=sys.stderr)
                print("=" * 60, file=sys.stderr)
            else:
                print("WARNING: Could not verify comment was posted", file=sys.stderr)

    except ClaudeInvocationError as e:
        print(f"ERROR: Claude Code failed: {e}", file=sys.stderr)
        if verbose:
            traceback.print_exc(file=sys.stderr)

        # Try to restore original branch
        if original_branch:
            try:
                checkout_branch(repo_path, original_branch)
            except Exception:
                pass
        return 4

    # ─── Restore original branch ─────────────────────────────────────────
    if original_branch:
        try:
            checkout_branch(repo_path, original_branch)
        except Exception:
            print(f"Warning: Could not restore original branch '{original_branch}'", file=sys.stderr)

    print("PR review complete!", file=sys.stderr)
    log_command("code-review plugin completed successfully", verbose=verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())