"""review.py — CLI entry point and orchestration for pr-review-performance.

mesh pr-review-performance <pr_url> [flags]
"""

import argparse
import sys

from pr_review_performance.chunker import build_chunks
from pr_review_performance.config import load_config
from pr_review_performance.formatter import render_report
from pr_review_performance.git import detect_repo_slug, get_diff, get_pr_description
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

    print("Fetching diff...", flush=True)
    try:
        diff = get_diff(config.base_branch, config.repo_root)
    except Exception as e:
        print(f"ERROR: failed to get diff: {e}", file=sys.stderr)
        return 1

    pr_desc = ""
    if args.pr_url:
        import subprocess as _subprocess
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
