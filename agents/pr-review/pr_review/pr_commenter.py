"""PR commenter for PR review agent - posts review to GitHub."""

import re
import sys
from pr_review.reviewer import Review, InlineComment
from pr_review.github_client import GitHubClient


SEVERITY_PREFIX = {
    "error": "🔴 **Error:**",
    "warning": "🟡 **Warning:**",
    "suggestion": "🔵 **Suggestion:**",
}

VERDICT_EMOJI = {
    "approved": "✅",
    "changes_requested": "❌",
    "comment": "💬",
}

VERDICT_LABEL = {
    "approved": "SUCCESS",
    "changes_requested": "FAIL",
    "comment": "COMMENT",
}


def parse_diff_positions(diff: str) -> dict[tuple[str, int], int]:
    """Parse unified diff to build mapping from (filename, file_line) -> diff_position.

    Args:
        diff: Unified diff string

    Returns:
        Dict mapping (filename, file_line_number) -> diff_position (1-indexed line in diff)
    """
    position_map = {}

    # Split diff into file hunks
    # Format: diff --git a/path/file b/path/file
    #         --- a/path/file
    #         +++ b/path/file
    #         @@ -X,Y +X,Y @@ optional context
    #         ...

    lines = diff.split('\n')
    current_file = None
    file_line_map = {}  # maps file_line -> diff_position

    i = 0
    while i < len(lines):
        line = lines[i]

        # Detect new file diff header
        diff_match = re.match(r'diff --git a/(.+?) b/(.+?)$', line)
        if diff_match:
            # Save previous file's map
            if current_file and file_line_map:
                for (fl, dp) in file_line_map.items():
                    position_map[(current_file, fl)] = dp

            current_file = diff_match.group(2)
            file_line_map = {}
            i += 1

            # Skip lines until we hit the hunk header (@@)
            while i < len(lines) and not lines[i].startswith('@@'):
                i += 1
            if i >= len(lines):
                break

            # Parse the hunk header to get starting line numbers
            # Format: @@ -X,Y +X,Y @@
            hunk_match = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', lines[i])
            if hunk_match:
                file_start_line = int(hunk_match.group(1))
            else:
                # Malformed hunk header — skip this hunk to avoid stale file_start_line
                while i < len(lines) and not lines[i].startswith('@@') and not lines[i].startswith('diff --git'):
                    i += 1
                continue

            diff_position = i + 1  # 1-indexed position in diff
            i += 1

            # Process hunk lines
            while i < len(lines):
                l = lines[i]
                if l.startswith('@@'):
                    # New hunk starts, parse its offset
                    hunk_match = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', l)
                    if hunk_match:
                        file_start_line = int(hunk_match.group(1))
                    diff_position = i + 1
                    # Don't reset file_line_map — accumulate mappings across all hunks of this file
                    i += 1
                    continue
                if l.startswith('diff --git'):
                    # Next file starts, break
                    break

                # Track lines added (those starting with '+' but not '+++')
                if l.startswith('+') and not l.startswith('+++'):
                    file_line_map[file_start_line] = diff_position
                    file_start_line += 1
                elif l.startswith('-'):
                    # Deleted lines - don't increment file line
                    pass
                elif not l.startswith('\\'):
                    # Context or nothing (blank lines, etc) - increment file line
                    file_start_line += 1

                diff_position += 1
                i += 1
            continue
        i += 1

    # Save last file
    if current_file and file_line_map:
        for (fl, dp) in file_line_map.items():
            position_map[(current_file, fl)] = dp

    return position_map


def format_summary_comment(review: Review, file_level_comments: list[dict] = None) -> str:
    """Format the summary comment for GitHub PR."""
    if file_level_comments is None:
        file_level_comments = []
    verdict_emoji = VERDICT_EMOJI.get(review.verdict, "💬")
    verdict_label = VERDICT_LABEL.get(review.verdict, "COMMENT")

    summary_parts = [review.summary]

    # Append file-level comments to the summary, grouped by severity
    if file_level_comments:
        by_severity = {"error": [], "warning": [], "suggestion": []}
        for comment in file_level_comments:
            # Extract severity from the comment body prefix
            body = comment["body"]
            if body.startswith("🔴"):
                by_severity["error"].append(comment)
            elif body.startswith("🟡"):
                by_severity["warning"].append(comment)
            else:
                by_severity["suggestion"].append(comment)

        summary_parts.append("\n---\n### 📝 File-level comments\n")
        for severity in ["error", "warning", "suggestion"]:
            comments = by_severity[severity]
            if not comments:
                continue
            heading = {
                "error": "🔴 Errors",
                "warning": "🟡 Warnings",
                "suggestion": "🔵 Suggestions",
            }[severity]
            summary_parts.append(f"\n#### {heading}\n")
            for comment in comments:
                summary_parts.append(f"- **{comment['path']}**: {comment['body']}")

    comment = f"""## 🤖 AI Code Review

{"".join(summary_parts)}

---
**Verdict:** {verdict_label} {verdict_emoji} {review.verdict}

<sub>Generated by pr-review using Claude Code with full repository context.</sub>
"""
    return comment


def format_inline_comments(
    review: Review,
    commit_sha: str,
    diff: str = "",
) -> tuple[list[dict], list[dict]]:
    """Format inline comments for GitHub PR review API.

    Args:
        review: The Review object with inline comments
        commit_sha: The commit SHA to comment on
        diff: Unified diff string for mapping file lines to diff positions

    Returns:
        Tuple of (line_comments, file_level_comments) for the GitHub API.
        File-level comments (position=0) are returned separately for summary inclusion.
    """
    # Build a set of lock files and config files to exclude
    excluded_patterns = [
        r".*\.lock$",
        r".*\.toml$",
        r".*pnpm-lock\.yaml$",
    ]

    def is_excluded(filename: str) -> bool:
        import fnmatch
        for pattern in excluded_patterns:
            if fnmatch.fnmatch(filename, pattern):
                return True
        return False

    # Parse diff to get position mapping
    position_map = parse_diff_positions(diff) if diff else {}

    line_comments = []
    file_level_comments = []
    for comment in review.inline_comments:
        # Skip excluded files (lock files, config)
        if is_excluded(comment.file):
            continue

        # Map severity to prefix
        prefix = SEVERITY_PREFIX.get(comment.severity, "🔵 **Suggestion:**")

        comment_body = f"{prefix} {comment.comment}"

        # Build the comment dict
        # GitHub PR review API requires:
        # - path: file path
        # - position: line in the unified diff (NOT the file line)
        # Only include comments where we have a valid diff position mapping.
        # Context lines (lines that weren't changed) have no position in the diff.
        key = (comment.file, comment.line)
        if key not in position_map:
            # Post as file-level comment (position 0)
            comment_dict = {
                "path": comment.file,
                "position": 0,
                "body": comment_body,
            }
            file_level_comments.append(comment_dict)
            continue

        position = position_map[key]

        comment_dict = {
            "path": comment.file,
            "position": position,
            "body": comment_body,
        }
        line_comments.append(comment_dict)

    return line_comments, file_level_comments


def post_review(
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    pr: dict,
    review: Review,
    dry_run: bool = False,
    diff: str = "",
) -> str:
    """Post the review to GitHub PR.

    Returns the combined comment body string (for --show-pr-comment).
    """
    """Post the review to GitHub PR.

    Args:
        github: GitHubClient instance
        owner: Repo owner
        repo: Repo name
        pr_number: PR number
        pr: PR metadata dict (for commit SHA)
        review: Review object to post
        dry_run: If True, print instead of posting
        diff: Unified diff string for mapping line numbers to diff positions
    """
    commit_sha = pr.get("head", {}).get("sha", "")

    # Format comments - separate line-level and file-level
    line_comments, file_level_comments = format_inline_comments(review, commit_sha, diff)
    summary = format_summary_comment(review, file_level_comments)

    if dry_run:
        print("=" * 60, file=None)
        print("DRY RUN - Would post the following review:", file=None)
        print("=" * 60, file=None)
        print(summary, file=None)
        print(file=None)
        if review.inline_comments:
            print(f"Would post {len(line_comments)} line comments and {len(file_level_comments)} file-level comments:", file=None)
            for comment in line_comments:
                body = comment.get("body", "")
                snippet = body.split(":**", 1)[0] if ":**" in body else body[:80]
                print(f"  [{comment.get('path')}:{comment.get('position')}] {snippet}", file=None)
            for comment in file_level_comments:
                body = comment.get("body", "")
                snippet = body.split(":**", 1)[0] if ":**" in body else body[:80]
                print(f"  [{comment.get('path')}:file-level] {snippet}", file=None)
        print("=" * 60, file=None)
        return

    # Post all comments as ONE aggregated PR comment
    all_lines = []
    if summary:
        all_lines.append(summary)

    for comment in line_comments:
        all_lines.append(f"- **[{comment['path']}:{comment['position']}]:** {comment['body']}")

    for comment in file_level_comments:
        all_lines.append(f"- **{comment['path']} (file-level):** {comment['body']}")

    combined = "\n".join(all_lines)

    github.create_pr_review(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        commit_sha=commit_sha,
        body=combined,
        comments=[],  # All comments are in the combined body markdown
    )

    return combined