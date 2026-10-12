"""Render issues as a Markdown report grouped by severity."""

import re

from review_pull_request.constants import REVIEW_SIGNATURE


MAX_LISTED_FILES = 50


def _code_span(text: str) -> str:
    """Markdown inline code for untrusted text: the fence is longer than any backtick run inside."""
    text = " ".join(text.splitlines())  # a newline would end the span (and a heading line)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"
SEVERITY_ORDER = ["high", "medium", "low"]
SEVERITY_EMOJI = {
    "high": "🔴",
    "medium": "🟡",
    "low": "🟢",
}
SEVERITY_LABEL = {
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}


def render_report(
    overview: str,
    issues: list,
    summaries: list[tuple[str, str]] | None = None,
    config=None,
    model: str | None = None,
    incremental_info: dict | None = None,
    incomplete: list[str] | None = None,
    excluded: list[str] | None = None,
    head_sha: str | None = None,
    missing: dict[str, list[str]] | None = None,
    listing_failed: bool = False,
    symlinks: list[str] | None = None,
) -> str:
    """Build a Markdown review report.

    Args:
        overview: Stage 1 overview text
        issues: list of Issue objects
        summaries: list of (file_path, summary) pairs
        config: Config instance
        model: effective model name (for the header)
        incremental_info: dict with 'base_sha' (the last reviewed commit) and 'commits'
            (SHAs reviewed since) for --since-last-review; if None, this is a full review.
        incomplete: files whose review did not complete; they are listed and the
            all-clear is withheld.
        excluded: changed files skipped by exclusion patterns; listed so the report
            does not imply coverage it does not have.
        head_sha: the PR head commit this report covers; recorded for --since-last-review.
        missing: paths the model asked to read that do not exist -> the files whose review asked.
        listing_failed: the repo file listing was unavailable, so the model got no path hints.
        symlinks: symlinks in the repo; warned about as a hidden dependency.
    """
    if summaries is None:
        summaries = []

    lines = []

    effective_model = model or config.model

    if incomplete:
        # No REVIEW_SIGNATURE: a partial review must never become a --since-last-review baseline.
        lines.append("# Partial pull request review\n")
    elif incremental_info:
        commits = incremental_info["commits"]
        lines.append(
            f"{REVIEW_SIGNATURE} (since last review: {incremental_info['base_sha'][:7]}.."
            f"{(head_sha or '')[:7]}, {len(commits)} new commit(s))\n"
        )
    else:
        lines.append(f"{REVIEW_SIGNATURE} (full review of {head_sha[:7]})\n" if head_sha
                     else f"{REVIEW_SIGNATURE} (full review)\n")

    # The next --since-last-review run starts from this commit (see incremental.reviewed_sha).
    if head_sha:
        lines.append(f"> **Reviewed commit:** `{head_sha}`")
    if incremental_info:
        lines.append("> **New commits:** " + " · ".join(f"`{c[:7]}`" for c in incremental_info["commits"]))

    files_reviewed = max(0, len(summaries) - len(incomplete or []))  # summaries include incomplete files
    files_reviewed_text = (
        f"{files_reviewed} of {len(summaries)}" if incomplete else str(files_reviewed)
    )
    lines.append(
        f"> **Model:** `{effective_model}`"
    )
    lines.append(
        f"> **Base branch:** `{config.base_branch}` · "
        f"**Files reviewed:** {files_reviewed_text}"
    )
    lines.append("\n---\n\n## Overview\n\n" + (overview or "Overview not available.") + "\n")

    high = [i for i in issues if i.severity == "high"]
    medium = [i for i in issues if i.severity == "medium"]
    low = [i for i in issues if i.severity == "low"]

    total = len(high) + len(medium) + len(low)
    lines.append("## Issues\n")
    if total == 0 and incomplete and files_reviewed == 0:
        lines.append("\nNo files were fully reviewed, so no issues can be reported.\n")
    elif total == 0 and incomplete:
        lines.append("\nNo issues found in the files that were reviewed.\n")
    elif total == 0:
        lines.append("\nNo issues found. ✅\n")
    else:
        lines.append(
            f"Found **{total}** issue(s): "
            f"{len(high)} 🔴 HIGH · "
            f"{len(medium)} 🟡 MEDIUM · "
            f"{len(low)} 🟢 LOW\n"
        )

        for severity in SEVERITY_ORDER:
            group = [i for i in issues if i.severity == severity]
            if not group:
                continue

            emoji = SEVERITY_EMOJI[severity]
            label = SEVERITY_LABEL[severity]
            lines.append(f"\n### {emoji} {label}\n")

            for iss in sorted(group, key=lambda i: i.file):
                line_str = f" · line {iss.line}" if iss.line else ""
                lines.append(f"#### {_code_span(iss.file)}{line_str}\n")
                lines.append(f"**Problem:** {iss.comment}\n")
                if iss.suggestion:
                    lines.append(f"**Fix:** {iss.suggestion}\n")
                lines.append("---\n")

    if incomplete:
        lines.append("\n## ⚠️ Not fully reviewed\n")
        lines.append(
            "The model did not return a usable review for these files, so they are "
            "**not** covered by the result above:\n"
        )
        lines.extend(f"- {_code_span(path)}" for path in incomplete[:MAX_LISTED_FILES])
        if len(incomplete) > MAX_LISTED_FILES:
            lines.append(f"- … and {len(incomplete) - MAX_LISTED_FILES} more")
        lines.append("")

    if excluded:
        shown = excluded[:MAX_LISTED_FILES]
        lines.append("\n## Excluded from review\n")
        lines.append(
            "These changed files matched an exclusion pattern and were **not** reviewed:\n"
        )
        lines.extend(f"- {_code_span(path)}" for path in shown)
        if len(excluded) > len(shown):
            lines.append(f"- … and {len(excluded) - len(shown)} more")
        lines.append("")

    if symlinks:
        lines.append("\n## ⚠️ Symlinks in the repository\n")
        lines.append(
            "The repository depends on these symlinks. A symlink is a hidden dependency: it can "
            "point outside the repository or at a file that is missing on another machine, and "
            "the review does not follow it:\n"
        )
        lines.extend(f"- {_code_span(p)}" for p in symlinks[:MAX_LISTED_FILES])
        if len(symlinks) > MAX_LISTED_FILES:
            lines.append(f"- … and {len(symlinks) - MAX_LISTED_FILES} more")
        lines.append("")

    if missing or listing_failed:
        lines.append("\n## Paths not found\n")
        if listing_failed:
            lines.append(
                "The repository file listing was unavailable, so the model was given no "
                "folder listings or suggestions for paths it could not find.\n"
            )
        if missing:
            lines.append("The model asked to read these paths, which do not exist at the reviewed commit:\n")
            for path, files in list(missing.items())[:MAX_LISTED_FILES]:
                asked = ", ".join(_code_span(f) for f in files)
                lines.append(f"- {_code_span(path)} (asked while reviewing {asked})")
            if len(missing) > MAX_LISTED_FILES:
                lines.append(f"- … and {len(missing) - MAX_LISTED_FILES} more")
        lines.append("")

    lines.append(
        f"\n<sub>Generated by `mesh review-pull-request` · model: `{effective_model}`</sub>"
    )

    return "\n".join(lines)
