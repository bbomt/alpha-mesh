"""Render issues as a Markdown report grouped by severity."""


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
) -> str:
    """Build a Markdown review report.

    Args:
        overview: Stage 1 overview text
        issues: list of Issue objects
        summaries: list of (file_path, summary) pairs
        config: Config instance
        model: effective model name (for the header)
    """
    if summaries is None:
        summaries = []

    lines = []

    effective_model = model or config.model

    lines.append("# AI Code Review\n")

    files_reviewed = len(summaries)
    lines.append(
        f"> **Models:** per-file=`{effective_model}` · "
        f"overview=`{config.effective_overview_model}`"
    )
    lines.append(
        f"> **Base branch:** `{config.base_branch}` · "
        f"**Files reviewed:** {files_reviewed}"
    )
    lines.append("\n---\n\n## Overview\n\n" + (overview or "Overview not available.") + "\n")

    high = [i for i in issues if i.severity == "high"]
    medium = [i for i in issues if i.severity == "medium"]
    low = [i for i in issues if i.severity == "low"]

    total = len(high) + len(medium) + len(low)
    lines.append("## Issues\n")
    if total == 0:
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
                lines.append(f"#### `{iss.file}`{line_str}\n")
                lines.append(f"**Problem:** {iss.comment}\n")
                if iss.suggestion:
                    lines.append(f"**Fix:** {iss.suggestion}\n")
                lines.append("---\n")

    return "\n".join(lines)
