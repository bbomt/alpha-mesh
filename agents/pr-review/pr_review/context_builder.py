"""Context builder for PR review agent - assembles prompt for Claude Code."""

from typing import Optional


EXCLUDED_PATTERNS = [
    r".*\.lock$",
    r".*\.toml$",
    r".*\.yaml$",
    r".*\.yml$",
    r".*\.json$",
    r".*\.env.*",
    r"tests?/.*",
    r".*_test\.py$",
    r".*\.test\.(js|ts)$",
    r".*\.spec\.(js|ts)$",
]


def build_review_prompt(
    pr: dict,
    diff: str,
    changed_files: list[dict],
    jira_ids: list[str],
    auto_fix: bool,
    repo_path: Optional[str] = None,
) -> str:
    """Assemble the review prompt for Claude Code.

    Args:
        pr: PR metadata dict (title, body, head branch, base branch, author)
        diff: Unified diff string
        changed_files: List of changed file dicts
        jira_ids: List of JIRA ticket IDs found
        auto_fix: Whether to allow Claude Code to make changes
        repo_path: Path to the repository (for context)

    Returns:
        Markdown prompt string
    """
    pr_title = pr.get("title", "")
    pr_body = pr.get("body") or ""
    head_branch = pr.get("head", {}).get("ref", "")
    base_branch = pr.get("base", {}).get("ref", "")
    pr_author = pr.get("user", {}).get("login", "")
    pr_url = pr.get("html_url", "")
    pr_state = pr.get("state", "")

    # Build changed files section
    files_text = ""
    for f in changed_files:
        filename = f.get("filename", "")
        status = f.get("status", "")
        files_text += f"- `{filename}` ({status})\n"

    # JIRA section
    jira_section = ""
    if jira_ids:
        jira_list = ", ".join(jira_ids)
        jira_section = f"""
## Linked JIRA Tickets

{jira_list}
Please look up these tickets using the Atlassian MCP plugin and use their content as background context for this review.
"""
    else:
        jira_section = "\n## Linked JIRA Tickets\n\nNo JIRA tickets found in PR title, branch name, or description.\n"

    # Auto-fix instruction
    if auto_fix:
        fix_instruction = (
            "You have permission to make changes to fix issues you identify. "
            "After making changes, describe what you changed and why."
        )
    else:
        fix_instruction = "Do NOT make any changes to files. This is a plan-mode review only."

    prompt = f"""## Pull Request: {pr_title}

**Branch:** {head_branch} → {base_branch}
**Author:** {pr_author}
**State:** {pr_state}
**URL:** {pr_url}

---

## PR Description

{pr_body}

{jira_section}
---

## Changed Files

{files_text}

---

## Code Changes (unified diff)

```diff
{diff}
```

---

## Review Instructions

You are performing a code review with full access to the codebase in the current directory.

The PR description and JIRA tickets above are **background context only** — they explain the intent of the change. Do not check whether the code exactly matches the JIRA spec; the implementation may be more refined.

Focus your review on:
* Correctness and logic errors
* Security issues
* Performance concerns
* Code clarity and maintainability
* Consistency with the surrounding codebase (use your access to the repo)
* Missing error handling

{fix_instruction}

## Output Format

Return your review in the following JSON structure:

{{
  "summary": "Overall markdown summary of the review (2-5 paragraphs)",
  "inline_comments": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "severity": "error|warning|suggestion",
      "comment": "Markdown comment text"
    }}
  ],
  "verdict": "approved|changes_requested|comment"
}}

Only include inline comments for source files. Do not comment on test files, configuration files, or lockfiles (e.g. package-lock.json, poetry.lock, *.yaml config, *.toml config).
"""
    return prompt
