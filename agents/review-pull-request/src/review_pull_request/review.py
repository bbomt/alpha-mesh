"""review.py — CLI entry point and orchestration for review-pull-request.

mesh review-pull-request <pr_url> [flags]
"""

import argparse
import sys
import time
from urllib.parse import urlparse

from review_pull_request.checkpoints import ThreadBusy, lock_thread, make_checkpointer, thread_id
from review_pull_request.config import load_config
from review_pull_request.formatter import _code_span, render_report
from review_pull_request.git import (
    get_diff,
    get_pr_description,
    get_pr_info,
    parse_pr_url,
)
from review_pull_request.github import _get_github_token, post_comment
from review_pull_request.graph import ReviewContext, build_review_graph, initial_state, next_file
from review_pull_request.llm import DRY_RUN_MODEL, LLMError
from review_pull_request.incremental import (
    find_last_review_comment,
    get_commits_between,
    build_incremental_diff,
    reviewed_sha,
)
from review_pull_request.session import ReviewSession, _safe_url
from review_pull_request.stage2 import Issue
from review_pull_request.tools import GitHubFileReader


class PostFailed(Exception):
    """GitHub did not accept the review comment."""


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
        "by the exclusion patterns (`.pr-review-ignore`, `review_config.toml` or the built-in "
        "sensitive-file rules), so this pull request has **not** been reviewed.",
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


def _verbose_print(config, *args, **kwargs):
    if config.verbose:
        print(*args, **kwargs, file=sys.stderr, flush=True)


def main(argv=None, *, checkpointer=None) -> int:
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
  mesh review-pull-request https://github.com/org/repo/pull/123 --force-rerun

Rerunning the same command after a failure resumes from the file it stopped at.
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

    parser.add_argument(
        "--force-rerun",
        action="store_true",
        help="Discard the saved progress of this review (same PR, commit and mode) and start again",
    )

    args = parser.parse_args(argv)

    config = load_config(args)
    print(
        f"Model: {config.model}\n"
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
    # Kept local, never on config: stages see only the policy-wrapped reader in the session.
    github_reader = GitHubFileReader(owner, repo, head_sha, _token)
    if args.dry_run:
        config.model = DRY_RUN_MODEL  # the session gets the scripted dry-run model, no network
    # Everything that reaches the model or the file reader goes through this session's policy.
    session = ReviewSession.create(config, inner_reader=github_reader, secrets=[_token])
    _verbose_print(config, f"read_file: GitHub API (ref={head_sha[:8]})")

    # Print model info if verbose
    _verbose_print(config, f"Model: {config.model}")
    _verbose_print(config, f"API URL: {_safe_url(config.api_url)}")

    start_time = time.time()
    incremental_info = None
    current_step = "review"
    incomplete_files: list[str] = []
    prior_review_exists = False
    run_lock = None
    kept = ""
    try:
        # ── Incremental diff (--since-last-review) ──────────────────────────
        if args.since_last_review and not args.dry_run:
            last_review = find_last_review_comment(owner, repo, pr_number, _token)
            prior_review_exists = bool(last_review)
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
                last_sha = reviewed_sha(last_review.get("body", ""))
                new_commits = None
                if not last_sha:
                    print(
                        "The last review does not record its commit — running full review.",
                        file=sys.stderr,
                    )
                elif last_sha == head_sha:
                    print(
                        f"No new commits since last review ({last_sha[:7]}). Nothing to review.",
                        file=sys.stderr,
                    )
                    return 0
                else:
                    new_commits = get_commits_between(owner, repo, last_sha, head_sha, _token)
                    if new_commits is None:
                        print(
                            "WARNING: cannot list commits since the last review — running full review.",
                            file=sys.stderr,
                        )
                    elif not new_commits:
                        print(
                            f"No new commits since last review ({last_sha[:7]}). Nothing to review.",
                            file=sys.stderr,
                        )
                        return 0

                if new_commits:
                    print(
                        f"Building diff since last review ({last_sha[:7]}..{head_sha[:7]}, "
                        f"{len(new_commits)} new commit(s))...",
                        file=sys.stderr,
                    )
                    diff = build_incremental_diff(owner, repo, last_sha, head_sha, _token)
                    if diff:
                        incremental_info = {
                            "base_sha": last_sha,
                            "commits": [c["sha"] for c in new_commits],
                        }
                    else:
                        print(
                            "WARNING: incremental diff is empty — running full review.",
                            file=sys.stderr,
                        )
                if not incremental_info:
                    print("Fetching full diff...", file=sys.stderr)
                    try:
                        diff = get_diff(owner, repo, pr_number)
                    except Exception as e:
                        print(f"ERROR: failed to get diff: {e}", file=sys.stderr)
                        return 1
        else:
            print("Fetching diff...", file=sys.stderr)
            try:
                diff = get_diff(owner, repo, pr_number)
            except Exception as e:
                print(f"ERROR: failed to get diff: {e}", file=sys.stderr)
                return 1

        safe = session.safe_diff(diff)
        chunks = list(safe.chunks)
        if not chunks:
            all_paths = list(safe.excluded)
            if not all_paths:
                print("No files to review (diff is empty).", file=sys.stderr)
                return 0
            if incremental_info or prior_review_exists:
                # The PR already has a review; new commits only touch excluded files. Saying
                # "this PR has not been reviewed" would be false, and the notice (which is not a
                # baseline) would be re-posted on every later incremental run.
                print(
                    f"No new reviewable changes: the {len(all_paths)} file(s) changed since the "
                    "last review are all excluded by exclusion patterns.",
                    file=sys.stderr,
                )
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

        excluded_files = list(safe.excluded)
        print(f"Diff: {len(chunks)} file chunk(s) to review", file=sys.stderr)
        if args.dry_run:
            print("=== review-pull-request [DRY-RUN] ===", file=sys.stderr)

        post_to_pr = not args.dry_run and not args.no_post_comment and args.pr_url
        show_comment = not args.no_output and not (args.hide_pr_comment and post_to_pr)

        def render(state) -> str:
            results = [state["results"][f] for f in state["files"]]
            missing: dict[str, list[str]] = {}
            for r in results:
                for path in r["missing"]:
                    missing.setdefault(path, []).append(r["file_path"])
            incomplete_files[:] = [r["file_path"] for r in results if r["incomplete"]]  # idempotent node
            # Before listing_failed is read: this may be the first (and only) fetch of the repo tree.
            repo_symlinks = [] if args.dry_run else session.symlinks()
            return render_report(
                overview=state["overview"],
                issues=[Issue(**i) for r in results for i in r["issues"]],
                issue_models=[r["model"] for r in results for _ in r["issues"]],
                summaries=[(r["file_path"], r["summary"]) for r in results],
                config=config,
                model=config.model,
                incremental_info=incremental_info,
                head_sha=head_sha,
                incomplete=incomplete_files,
                excluded=excluded_files,
                missing=missing,
                listing_failed=session.listing_failed,
                symlinks=repo_symlinks,
                overview_model=state["overview_model"],
                file_models={r["file_path"]: r["model"] for r in results},
            )

        def publish(report: str) -> None:
            if show_comment:
                print(report)
            if post_to_pr:
                print("Posting to PR...", file=sys.stderr)
                if not post_comment(report, args.pr_url, config):
                    raise PostFailed()

        mode = f"since-{incremental_info['base_sha']}" if incremental_info else "full"
        thread = thread_id(owner, repo, pr_number, head_sha, mode, config.exclusions)
        if not args.dry_run:
            try:
                run_lock = lock_thread(thread)
            except ThreadBusy as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1
        if checkpointer is None:
            checkpointer = make_checkpointer(dry_run=args.dry_run)
        graph = build_review_graph(checkpointer)
        run_config = {"configurable": {"thread_id": thread}}
        if args.force_rerun:
            checkpointer.delete_thread(thread)
        state = graph.get_state(run_config)
        if state.values and not state.next:
            print(f"Already completed (thread {thread}): printing the stored report, posting nothing.\n"
                  "  Use --force-rerun to review this commit again.", file=sys.stderr)
            if not args.no_output:
                print(state.values["report"])
            return 1 if any(r["incomplete"] for r in state.values["results"].values()) else 0
        if state.values and state.values["files"] != [c.file_path for c in chunks]:
            # Same head SHA, other diff: e.g. the PR was retargeted or its base branch rewritten.
            print(f"ERROR: the PR's changed files differ from the saved review (thread {thread}).\n"
                  "  Run again with --force-rerun to review the current diff from the start.", file=sys.stderr)
            return 1
        if state.values:
            done = [f for f in state.values["files"] if f in state.values["results"]]
            todo = [f for f in state.values["files"] if f not in state.values["results"]]
            print(f"Resuming: {len(done)} file(s) from checkpoint, {len(todo)} to review (thread {thread})\n"
                  f"  From checkpoint: {', '.join(done) or '(none)'}\n"
                  f"  To review now: {', '.join(todo) or '(none)'}", file=sys.stderr)
            graph_input = None
        else:
            print(f"Starting review (thread {thread})", file=sys.stderr)
            graph_input = initial_state(safe)
        context = ReviewContext(session=session, diff=safe, pr_description=pr_desc,
                                model=config.model, render=render, publish=publish)
        try:
            # sync: each node's checkpoint is saved before the next node starts, so a kill loses
            # at most the file in flight.
            graph.invoke(graph_input, run_config, context=context, durability="sync")
        except PostFailed:
            print("ERROR: could not post the review to the PR (see warning above).", file=sys.stderr)
            return 1
        except LLMError:
            state = graph.get_state(run_config)
            pending = next_file(state.values) if "review_next_file" in state.next else None
            current_step = pending or "overview pass"
            if state.values.get("overview") and not args.dry_run:
                kept = (f"  Completed work is kept: overview + {len(state.values['results'])} file(s). "
                        "Run the same command to resume (--model may name another model).\n")
            raise

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
        print(
            f"ERROR: LLM API call failed during {current_step}: {e}\n"
            f"  model: {config.model}\n"
            f"  api host: {host}\n"
            "  The model API is unusable, so the review was aborted and nothing was posted.\n"
            f"{kept}"
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
        github_reader.close()
        if run_lock:
            run_lock.close()


if __name__ == "__main__":
    sys.exit(main())