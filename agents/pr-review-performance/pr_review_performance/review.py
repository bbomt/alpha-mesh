"""review.py — CLI entry point and orchestration for pr-review-performance.

mesh pr-review-performance <pr_url> [flags]
"""

import argparse
import sys
import time

from pr_review_performance.chunker import build_chunks
from pr_review_performance.config import load_config
from pr_review_performance.formatter import render_report
from pr_review_performance.git import (
    checkout_branch,
    get_current_branch,
    get_diff,
    get_pr_description,
    get_pr_info,
    parse_pr_url,
    pull_latest,
)
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


def _verbose_print(config, *args, **kwargs):
    if config.verbose:
        print(*args, **kwargs, file=sys.stderr, flush=True)


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
        "--hide-pr-comment",
        action="store_true",
        help="Post the report to GitHub but don't print it to stdout",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed logs to stderr",
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

    print(f"Repo path: {config.repo_root}", file=sys.stderr)

    # Parse PR URL to get owner/repo/pr_number
    try:
        owner, repo, pr_number = parse_pr_url(args.pr_url)
    except Exception as e:
        print(f"ERROR: could not parse PR URL '{args.pr_url}': {e}", file=sys.stderr)
        return 1

    print(f"PR: {owner}/{repo}#{pr_number}", file=sys.stderr)
    print("Fetching PR data from GitHub...", file=sys.stderr)

    # Fetch PR info (branch names, title)
    pr_info = get_pr_info(owner, repo, pr_number)
    if not pr_info:
        print(f"ERROR: could not fetch PR info for {owner}/{repo}/pull/{pr_number}", file=sys.stderr)
        return 1

    pr_head_branch = pr_info.get("headRefName", "")

    # Get PR description
    import subprocess as _subprocess
    _result = _subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    _token = _result.stdout.strip() if _result.returncode == 0 else ""
    pr_desc = get_pr_description(args.pr_url, _token)
    if pr_desc:
        print("Fetched PR description.", file=sys.stderr)

    # Print model info if verbose
    _verbose_print(config, f"Model: {config.model}")
    _verbose_print(config, f"API URL: {config.api_url}")

    start_time = time.time()
    original_branch = None
    try:
        # Auto-checkout PR branch if not already on it (skip for dry-run)
        if not args.dry_run:
            current_branch = get_current_branch(config.repo_root)
            if current_branch != pr_head_branch:
                print(f"Checking out PR branch: {pr_head_branch}", file=sys.stderr)
                _verbose_print(config, f"  original branch: {current_branch}")
                checkout_branch(config.repo_root, pr_head_branch)
                pull_latest(config.repo_root, pr_head_branch)
                original_branch = current_branch
            else:
                _verbose_print(config, f"Already on branch: {pr_head_branch}")
                original_branch = current_branch

        print("Fetching diff...", file=sys.stderr)
        try:
            diff = get_diff(owner, repo, pr_number)
        except Exception as e:
            print(f"ERROR: failed to get diff: {e}", file=sys.stderr)
            return 1

        chunks = build_chunks(diff, config.exclusions)
        if not chunks:
            print("No files to review (diff empty or all files excluded).", file=sys.stderr)
            return 0

        print(f"Diff: {len(chunks)} file chunk(s) to review", file=sys.stderr)

        if args.dry_run:
            print("=== pr-review-performance [DRY-RUN] ===", file=sys.stderr)
            overview = MOCK_OVERVIEW
            all_issues = []
            all_summaries = []
            for i, chunk in enumerate(chunks):
                print(f"[{i+1}/{len(chunks)}] reviewing {chunk.file_path}...", file=sys.stderr)
                all_summaries.append((chunk.file_path, MOCK_FILE_RESULT["summary"]))
            model = DRY_RUN_MODEL
        else:
            print("Stage 1: Overview pass...", file=sys.stderr)
            overview = run_overview(diff, pr_desc, config)
            print(f"Overview: {overview[:80]}...", file=sys.stderr)

            all_issues = []
            all_summaries = []
            for i, chunk in enumerate(chunks):
                print(f"[{i+1}/{len(chunks)}] reviewing {chunk.file_path}...", file=sys.stderr)
                result = run_file_review(chunk, overview, config)
                all_issues.extend(result.issues)
                all_summaries.append((chunk.file_path, result.summary))
                print(
                    f"  → {len(result.issues)} issue(s), "
                    f"{result.tool_calls_made} tool call(s)", file=sys.stderr,
                )
            model = config.model

        report = render_report(
            overview=overview,
            issues=all_issues,
            summaries=all_summaries,
            config=config,
            model=model,
        )

        # stdout: hide_pr_comment means print body to stdout only if NOT hiding
        post_to_pr = not args.dry_run and not args.no_post_comment and args.pr_url
        show_comment = not args.no_output and not (args.hide_pr_comment and post_to_pr)
        if show_comment:
            print(report)

        if post_to_pr:
            print("Posting to PR...", file=sys.stderr)
            post_comment(report, args.pr_url, config)

        elapsed = time.time() - start_time
        mins, secs = divmod(int(elapsed), 60)
        print(f"PR review complete! Reviewed {len(chunks)} file(s) in {mins}m {secs}s.", file=sys.stderr)
        return 0

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    finally:
        # Restore original branch
        if original_branch and not args.dry_run:
            try:
                checkout_branch(config.repo_root, original_branch)
                _verbose_print(config, f"Restored original branch: {original_branch}")
            except Exception as ex:
                print(f"Warning: Could not restore original branch '{original_branch}': {ex}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())