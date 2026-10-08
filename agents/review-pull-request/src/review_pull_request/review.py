"""review.py — CLI entry point and orchestration for review-pull-request.

mesh review-pull-request <pr_url> [flags]
"""

import argparse
import sys
import time
from urllib.parse import urlparse

from review_pull_request.chunker import build_chunks, chunk_exists
from review_pull_request.config import load_config
from review_pull_request.formatter import _code_span, render_report
from review_pull_request.git import (
    get_diff,
    get_pr_description,
    get_pr_info,
    parse_pr_url,
)
from review_pull_request.github import _get_github_token, post_comment
from review_pull_request.llm import LLMError
from review_pull_request.incremental import (
    find_last_review_comment,
    get_commits_since,
    build_incremental_diff,
)
from review_pull_request.stage1 import run_overview
from review_pull_request.stage2 import run_file_review
from review_pull_request.tools import GitHubFileReader


DRY_RUN_MODEL = "dry-run-model"


def _safe_url(url: str) -> str:
    """URL without userinfo or query string, safe to print."""
    try:
        p = urlparse(url)
    except ValueError:  # e.g. unbalanced IPv6 brackets
        return "(invalid URL)"
    if not p.hostname:
        return "(invalid URL)"
    try:
        port = f":{p.port}" if p.port else ""
    except ValueError:  # malformed port in a hand-edited .env
        return "(invalid URL)"
    return f"{p.scheme}://{p.hostname}{port}{p.path}"


MAX_LISTED_FILES = 50


def _excluded_notice(paths: list[str]) -> str:
    """PR comment for a PR whose every changed file was excluded.

    Deliberately does not contain REVIEW_SIGNATURE, so --since-last-review never
    mistakes it for a completed review.
    """
    shown = paths[:MAX_LISTED_FILES]
    lines = [
        "# Pull request not reviewed",
        "",
        f"No files were reviewed: all {len(paths)} changed file(s) were excluded "
        "by the exclusion patterns (`.pr-review-ignore` / `review_config.toml`), so this "
        "pull request has **not** been reviewed.",
        "",
    ]
    lines.extend(f"- {_code_span(p)}" for p in shown)
    if len(paths) > len(shown):
        lines.append(f"- … and {len(paths) - len(shown)} more")
    return "\n".join(lines)


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
        description="review-pull-request — LLM-powered code review with full codebase context",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  mesh review-pull-request https://github.com/org/repo/pull/123
  mesh review-pull-request https://github.com/org/repo/pull/123 --base main
  mesh review-pull-request https://github.com/org/repo/pull/123 --dry-run
  mesh review-pull-request https://github.com/org/repo/pull/123 --no-post-comment
  mesh review-pull-request https://github.com/org/repo/pull/123 --since-last-review
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
    parser.add_argument(
        "--since-last-review",
        action="store_true",
        help=(
            "Only review changes made since the last AI review comment on this PR. "
            "Falls back to a full review if no previous review is found."
        ),
    )

    args = parser.parse_args()

    config = load_config(args)
    print(
        f"Model: {config.model} (overview: {config.effective_overview_model})\n"
        f"API URL: {_safe_url(config.api_url)}",
        file=sys.stderr,
    )

    # Parse PR URL to get owner/repo/pr_number
    try:
        owner, repo, pr_number = parse_pr_url(args.pr_url)
    except Exception as e:
        print(f"ERROR: could not parse PR URL '{args.pr_url}': {e}", file=sys.stderr)
        return 1

    print(f"Reviewing {owner}/{repo} PR #{pr_number}", file=sys.stderr)
    print("Fetching PR data from GitHub...", file=sys.stderr)

    # Fetch PR info (branch names, head SHA)
    pr_info = get_pr_info(owner, repo, pr_number)
    if not pr_info:
        print(f"ERROR: could not fetch PR info for {owner}/{repo}/pull/{pr_number}", file=sys.stderr)
        return 1

    pr_head_branch = pr_info.get("headRefName", "")
    print(f"Branch: {pr_head_branch}", file=sys.stderr)

    # Get GitHub token and PR description
    _token = _get_github_token() or ""
    pr_desc = get_pr_description(args.pr_url, _token)
    if pr_desc:
        print("Fetched PR description.", file=sys.stderr)

    # Always read files via GitHub API at the PR head SHA
    head_sha = pr_info.get("headRefOid", "")
    if not _token:
        print(
            "ERROR: no GitHub token found. Run 'gh auth login' (or set GITHUB_TOKEN) and try again.",
            file=sys.stderr,
        )
        return 1
    if not head_sha:
        print(
            "ERROR: could not determine PR head SHA from GitHub API response.",
            file=sys.stderr,
        )
        return 1
    config.github_reader = GitHubFileReader(owner, repo, head_sha, _token)
    _verbose_print(config, f"read_file: GitHub API (ref={head_sha[:8]})")

    # Print model info if verbose
    _verbose_print(config, f"Model: {config.model}")
    _verbose_print(config, f"API URL: {_safe_url(config.api_url)}")

    start_time = time.time()
    incremental_info = None
    current_step = "review"
    incomplete_files: list[str] = []
    try:
        # ── Incremental diff (--since-last-review) ──────────────────────────
        if args.since_last_review and not args.dry_run:
            last_review = find_last_review_comment(owner, repo, pr_number, _token)
            if not last_review:
                print(
                    "No previous review found — running full review.",
                    file=sys.stderr,
                )
                print("Fetching full diff...", file=sys.stderr)
                try:
                    diff = get_diff(owner, repo, pr_number)
                except Exception as e:
                    print(f"ERROR: failed to get diff: {e}", file=sys.stderr)
                    return 1
            else:
                review_ts = last_review["created_at"]
                new_commits = get_commits_since(owner, repo, pr_number, review_ts, _token)
                if new_commits is None:
                    # API error — fall back to full review rather than silently skipping
                    print(
                        "WARNING: failed to fetch commits, falling back to full review.",
                        file=sys.stderr,
                    )
                    print("Fetching full diff...", file=sys.stderr)
                    try:
                        diff = get_diff(owner, repo, pr_number)
                    except Exception as e:
                        print(f"ERROR: failed to get diff: {e}", file=sys.stderr)
                        return 1
                elif not new_commits:
                    from datetime import datetime
                    dt = datetime.fromisoformat(review_ts.replace("Z", "+00:00"))
                    formatted = dt.strftime("%Y-%m-%d %H:%M")
                    print(
                        f"No new commits since last review ({formatted}). Nothing to review.",
                        file=sys.stderr,
                    )
                    return 0
                else:
                    # New commits found — build diff from parent of first new commit to head
                    head_sha = pr_info.get("headRefOid", "")
                    parents = new_commits[0].get("parents", [])
                    base_sha = parents[0]["sha"] if parents else new_commits[0]["sha"]

                    print(
                        f"Building incremental diff ({len(new_commits)} new commit(s) "
                        f"since {review_ts[:10]})...",
                        file=sys.stderr,
                    )
                    diff = build_incremental_diff(owner, repo, base_sha, head_sha, _token)
                    if not diff:
                        print(
                            "WARNING: incremental diff is empty, falling back to full review.",
                            file=sys.stderr,
                        )
                        try:
                            diff = get_diff(owner, repo, pr_number)
                        except Exception as e:
                            print(f"ERROR: failed to get diff: {e}", file=sys.stderr)
                            return 1
                    else:
                        incremental_info = {
                            "commit_count": len(new_commits),
                            "since_timestamp": review_ts,
                        }
        else:
            print("Fetching diff...", file=sys.stderr)
            try:
                diff = get_diff(owner, repo, pr_number)
            except Exception as e:
                print(f"ERROR: failed to get diff: {e}", file=sys.stderr)
                return 1

        all_chunks = build_chunks(diff, [])
        chunks = [c for c in all_chunks if not chunk_exists(c.file_path, config.exclusions)[1]]
        if not chunks:
            all_paths = [c.file_path for c in all_chunks]
            if not all_paths:
                print("No files to review (diff is empty).", file=sys.stderr)
                return 0
            notice = _excluded_notice(all_paths)
            print(
                f"No files to review: all {len(all_paths)} changed file(s) were excluded "
                "by exclusion patterns.",
                file=sys.stderr,
            )
            will_post = not args.dry_run and not args.no_post_comment and args.pr_url
            if not args.no_output and not (args.hide_pr_comment and will_post):
                print(notice)
            if not args.dry_run and not args.no_post_comment and args.pr_url:
                if not post_comment(notice, args.pr_url, config):
                    print("ERROR: could not post the 'not reviewed' notice to the PR.", file=sys.stderr)
                    return 1
            return 0

        reviewed_paths = {c.file_path for c in chunks}
        excluded_files = [c.file_path for c in all_chunks if c.file_path not in reviewed_paths]
        print(f"Diff: {len(chunks)} file chunk(s) to review", file=sys.stderr)

        if args.dry_run:
            print("=== review-pull-request [DRY-RUN] ===", file=sys.stderr)
            overview = MOCK_OVERVIEW
            all_issues = []
            all_summaries = []
            for i, chunk in enumerate(chunks):
                print(f"[{i+1}/{len(chunks)}] reviewing {chunk.file_path}...", file=sys.stderr)
                all_summaries.append((chunk.file_path, MOCK_FILE_RESULT["summary"]))
            model = DRY_RUN_MODEL
        else:
            current_step = "overview pass"
            print("Stage 1: Overview pass...", file=sys.stderr)
            # the overview prompt gets only non-excluded files: nothing excluded may reach the LLM
            overview = run_overview("".join(c.diff_text for c in chunks), pr_desc, config)
            print(f"Overview: {overview[:80]}...", file=sys.stderr)

            all_issues = []
            all_summaries = []
            for i, chunk in enumerate(chunks):
                current_step = chunk.file_path
                print(f"[{i+1}/{len(chunks)}] reviewing {chunk.file_path}...", file=sys.stderr)
                result = run_file_review(chunk, overview, config)
                all_issues.extend(result.issues)
                all_summaries.append((chunk.file_path, result.summary))
                if result.incomplete:
                    incomplete_files.append(chunk.file_path)
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
            incremental_info=incremental_info,
            incomplete=incomplete_files,
            excluded=excluded_files,
        )

        # stdout: hide_pr_comment means print body to stdout only if NOT hiding
        post_to_pr = not args.dry_run and not args.no_post_comment and args.pr_url
        show_comment = not args.no_output and not (args.hide_pr_comment and post_to_pr)
        if show_comment:
            print(report)

        if post_to_pr:
            print("Posting to PR...", file=sys.stderr)
            if not post_comment(report, args.pr_url, config):
                print("ERROR: could not post the review to the PR (see warning above).", file=sys.stderr)
                return 1

        elapsed = time.time() - start_time
        mins, secs = divmod(int(elapsed), 60)
        print(f"PR review complete! Reviewed {len(chunks)} file(s) in {mins}m {secs}s.", file=sys.stderr)
        if incomplete_files:
            print(
                f"WARNING: {len(incomplete_files)} file(s) were not fully reviewed "
                f"(unusable model response): {', '.join(incomplete_files)}",
                file=sys.stderr,
            )
            return 1
        return 0

    except LLMError as e:
        try:
            host = urlparse(config.api_url).hostname or "(invalid URL)"
        except ValueError:
            host = "(invalid URL)"
        failed_model = config.effective_overview_model if current_step == "overview pass" else config.model
        print(
            f"ERROR: LLM API call failed during {current_step}: {e}\n"
            f"  model: {failed_model}\n"
            f"  api host: {host}\n"
            "  The model API is unusable, so the review was aborted and nothing was posted.\n"
            "  Check CODE_REVIEW_MODEL / CODE_REVIEW_URL / CODE_REVIEW_TOKEN in .env.",
            file=sys.stderr,
        )
        return 1

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    finally:
        config.read_cache.clear()
        config.read_cache_bytes = 0
        if config.github_reader is not None:
            config.github_reader.close()


if __name__ == "__main__":
    sys.exit(main())