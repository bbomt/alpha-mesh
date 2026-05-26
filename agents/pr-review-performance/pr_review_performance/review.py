"""review.py — CLI entry point and orchestration for pr-review-performance.

mesh pr-review-performance <pr_url> [flags]
"""

import argparse
import sys

from pr_review_performance.chunker import build_chunks
from pr_review_performance.config import load_config
from pr_review_performance.formatter import render_report
from pr_review_performance.git import get_diff, get_pr_description, get_pr_info, parse_pr_url
from pr_review_performance.github import post_comment
from pr_review_performance.stage1 import run_overview
from pr_review_performance.stage2 import run_file_review


DRY_RUN_MODEL = "dry-run-model"


def _mock_llm_config(config):
    """Return a config identical to the given one but with a dry-run model marker."""
    class DryRunConfig:
        pass
    dc = DryRunConfig()
    for k, v in config.__dict__.items():
        setattr(dc, k, v)
    setattr(dc, "model", DRY_RUN_MODEL)
    return dc


MOCK_OVERVIEW = (
    "This pull request refactors the dream core module into a separate agent "
    "directory, following the established pattern from PMAT-189. It introduces "
    "a thin proxy at bin/dream that delegates to agents/dream/dream/bin/dream, "
    "while filtering out system-generated Claude Code messages from the session "
    "data fed to the LLM."
)

MOCK_FILE_RESULT = {
    "summary": "Delegates to the inner dream bash runner after loading .env "
               "credentials. Adds dry-run handling to skip env validation.",
    "issues": [],
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="pr-review-performance — LLM-powered code review with full codebase context",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  mesh pr-review-performance https://github.com/org/repo/pull/123
  mesh pr-review-performance https://github.com/org/repo/pull/123 --base main
  mesh pr-review-performance https://github.com/org/repo/pull/123 --dry-run
  mesh pr-review-performance https://github.com/org/repo/pull/123 --no-post-comment
""",
    )
    parser.add_argument(
        "pr_url",
        help="Full GitHub PR URL, e.g. https://github.com/org/repo/pull/123",
    )
    parser.add_argument(
        "--base",
        type=str,
        default=None,
        help="Base branch to diff against (default: master)",
    )
    parser.add_argument(
        "--no-output",
        action="store_true",
        help="Suppress printing the review report to stdout",
    )
    parser.add_argument(
        "--no-post-comment",
        action="store_true",
        help="Do not post the report as a GitHub PR comment",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run full pipeline with mocked LLM; skip posting to PR",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to TOML config file (default: review_config.toml)",
    )
    parser.add_argument(
        "--repo-path",
        type=str,
        default=None,
        help="Local path to the repository (default: current directory)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override CODE_REVIEW_MODEL from .env",
    )

    args = parser.parse_args()

    config = load_config(args)

    if args.dry_run:
        print("=== pr-review-performance [DRY-RUN] ===", flush=True)

    # Parse PR URL to get owner/repo/pr_number
    try:
        owner, repo, pr_number = parse_pr_url(args.pr_url)
    except Exception as e:
        print(f"ERROR: could not parse PR URL '{args.pr_url}': {e}", file=sys.stderr)
        return 1

    # Fetch PR info (branch names) and validate local repo matches
    pr_info = get_pr_info(owner, repo, pr_number)
    if not pr_info:
        print(f"ERROR: could not fetch PR info for {owner}/{repo}/pull/{pr_number}", file=sys.stderr)
        return 1

    import subprocess as _subprocess

    # Check local repo slug matches PR's repo
    local_slug_result = _subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, cwd=config.repo_root,
    )
    if local_slug_result.returncode == 0:
        remote_url = local_slug_result.stdout.strip()
        # Parse owner/repo from remote URL
        import re
        ssh_match = re.match(r"git@github\.com:([^/]+)/(.+?)(?:\.git)?$", remote_url)
        https_match = re.match(r"https://github\.com/([^/]+)/(.+?)(?:\.git)?$", remote_url)
        if ssh_match:
            local_slug = f"{ssh_match.group(1)}/{ssh_match.group(2)}"
        elif https_match:
            local_slug = f"{https_match.group(1)}/{https_match.group(2)}"
        else:
            local_slug = ""
        expected_slug = f"{owner}/{repo}"
        if local_slug and local_slug != expected_slug:
            print(
                f"ERROR: PR is from {expected_slug} but you are running from {local_slug}.\n"
                f"Clone {expected_slug} and run from there, or use --repo-path.",
                file=sys.stderr,
            )
            return 1

    # Check local branch matches PR's head branch
    branch_result = _subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, cwd=config.repo_root,
    )
    current_branch = branch_result.stdout.strip()
    pr_head_branch = pr_info.get("headRefName", "")
    if current_branch and pr_head_branch and current_branch != pr_head_branch:
        print(
            f"ERROR: PR branch is '{pr_head_branch}' but you are on '{current_branch}'.\n"
            f"Checkout the correct branch before running the review.",
            file=sys.stderr,
        )
        return 1

    print("Fetching diff...", flush=True)
    try:
        diff = get_diff(owner, repo, pr_number)
    except Exception as e:
        print(f"ERROR: failed to get diff: {e}", file=sys.stderr)
        return 1

    pr_desc = ""
    if args.pr_url:
        _result = _subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        _token = _result.stdout.strip() if _result.returncode == 0 else ""
        pr_desc = get_pr_description(args.pr_url, _token)
        if pr_desc:
            print("Fetched PR description.", flush=True)

    chunks = build_chunks(diff, config.exclusions)
    if not chunks:
        print("No files to review (diff empty or all files excluded).", flush=True)
        return 0

    print(f"Diff: {len(chunks)} file chunk(s) to review", flush=True)

    if args.dry_run:
        overview = MOCK_OVERVIEW
        all_issues = []
        all_summaries = []
        for chunk in chunks:
            all_summaries.append((chunk.file_path, MOCK_FILE_RESULT["summary"]))
        model = DRY_RUN_MODEL
    else:
        print("Stage 1: Overview pass...", flush=True)
        overview = run_overview(diff, pr_desc, config)
        print(f"Overview: {overview[:80]}...", flush=True)

        all_issues = []
        all_summaries = []
        for chunk in chunks:
            print(
                f"Stage 2: reviewing {chunk.file_path}...", flush=True,
            )
            result = run_file_review(chunk, overview, config)
            all_issues.extend(result.issues)
            all_summaries.append((chunk.file_path, result.summary))
            print(
                f"  → {len(result.issues)} issue(s), "
                f"{result.tool_calls_made} tool call(s)", flush=True,
            )
        model = config.model

    report = render_report(
        overview=overview,
        issues=all_issues,
        summaries=all_summaries,
        config=config,
        model=model,
    )

    if not args.no_output:
        print(report)

    if not args.dry_run and not args.no_post_comment and args.pr_url:
        print("Posting to PR...", flush=True)
        post_comment(report, args.pr_url, config)

    return 0


if __name__ == "__main__":
    sys.exit(main())
