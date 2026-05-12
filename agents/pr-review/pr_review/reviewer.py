"""Reviewer for PR review agent - parses Claude Code output into structured review."""

import json
import re
from dataclasses import dataclass, field
from typing import Optional


class ReviewParseError(Exception):
    """Exception raised when parsing review output fails."""
    pass


@dataclass
class InlineComment:
    """An inline comment on a specific file and line."""
    file: str
    line: int
    severity: str  # "error" | "warning" | "suggestion"
    comment: str


@dataclass
class Review:
    """Structured review output from Claude Code."""
    summary: str
    inline_comments: list[InlineComment] = field(default_factory=list)
    verdict: str = "comment"  # "approved" | "changes_requested" | "comment"


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


def _is_excluded_file(filename: str) -> bool:
    """Check if a file matches excluded patterns."""
    import fnmatch
    for pattern in EXCLUDED_PATTERNS:
        # Convert regex patterns to fnmatch-compatible where simple
        if fnmatch.fnmatch(filename, pattern):
            return True
        # Also try regex match
        if re.match(pattern, filename):
            return True
    return False


def parse_review_output(raw_output: str) -> Review:
    """Extract and parse the JSON review from Claude Code's output.

    The output may contain prose before/after the JSON — find the JSON object
    by looking for the first '{' and last '}' or use a regex.

    Args:
        raw_output: Raw stdout output from Claude Code

    Returns:
        Parsed Review object

    Raises:
        ReviewParseError: If parsing fails
    """
    if not raw_output or not raw_output.strip():
        raise ReviewParseError("Empty output from Claude Code")

    # Try to find JSON block in the output
    # First, try to find ```json ... ``` blocks
    json_match = re.search(r'```json\s*(.*?)\s*```', raw_output, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Try to find the first { and last } for a JSON object
        start = raw_output.find('{')
        end = raw_output.rfind('}')

        if start == -1 or end == -1 or start >= end:
            # Try to find JSON with ``` without json specifier
            json_match = re.search(r'```\s*(\{.*?\})\s*```', raw_output, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                raise ReviewParseError(
                    f"Could not find JSON in Claude Code output. Raw output:\n{raw_output[:500]}"
                )
        else:
            json_str = raw_output[start:end + 1]

    # Parse JSON
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ReviewParseError(
            f"Failed to parse JSON: {e}\nJSON string (first 500 chars):\n{json_str[:500]}"
        )

    # Validate required fields
    if not isinstance(data, dict):
        raise ReviewParseError("JSON root is not an object")

    summary = data.get("summary", "")
    if not summary:
        summary = "No summary provided by Claude Code."

    verdict = data.get("verdict", "comment")
    if verdict not in ("approved", "changes_requested", "comment"):
        verdict = "comment"

    # Parse inline comments
    inline_comments = []
    raw_comments = data.get("inline_comments", [])
    if not isinstance(raw_comments, list):
        raw_comments = []

    for idx, item in enumerate(raw_comments):
        if not isinstance(item, dict):
            continue

        if "file" not in item or "line" not in item or "comment" not in item:
            continue

        # Skip excluded files
        filename = item["file"]
        if _is_excluded_file(filename):
            continue

        severity = item.get("severity", "suggestion")
        if severity not in ("error", "warning", "suggestion"):
            severity = "suggestion"

        try:
            line = int(item["line"])
        except (ValueError, TypeError):
            continue

        inline_comments.append(
            InlineComment(
                file=filename,
                line=line,
                severity=severity,
                comment=item["comment"],
            )
        )

    return Review(
        summary=summary,
        inline_comments=inline_comments,
        verdict=verdict,
    )
