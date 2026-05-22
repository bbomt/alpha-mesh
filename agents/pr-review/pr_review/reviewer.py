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

    Args:
        raw_output: Raw stdout output from Claude Code

    Returns:
        Parsed Review object

    Raises:
        ReviewParseError: If parsing fails
    """
    if not raw_output or not raw_output.strip():
        raise ReviewParseError("Empty output from Claude Code")

    # Strip leading/trailing whitespace
    raw_output = raw_output.strip()

    # If output starts with { and ends with }, try parsing directly first
    if raw_output.startswith('{') and raw_output.endswith('}'):
        try:
            data = json.loads(raw_output)
            return _build_review(data)
        except json.JSONDecodeError:
            pass  # Fall through to more complex parsing

    # Try to find ```json ... ``` blocks
    json_match = re.search(r'```json\s*(\{.*?\})\s*```', raw_output, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            return _build_review(data)
        except json.JSONDecodeError:
            pass

    # Try to find ``` ... ``` blocks (without json specifier)
    # Use balanced brace counting to find the full JSON object
    json_str = _extract_json_from_fence(raw_output)
    if json_str:
        try:
            data = json.loads(json_str)
            return _build_review(data)
        except json.JSONDecodeError:
            pass

    # Try to find a JSON object using balanced brace matching
    json_str = _extract_json_object(raw_output)
    if json_str:
        try:
            data = json.loads(json_str)
            return _build_review(data)
        except json.JSONDecodeError:
            pass

    raise ReviewParseError(
        f"Could not find valid JSON in Claude Code output. Raw output (first 500 chars):\n{raw_output[:500]}"
    )


def _extract_json_from_fence(text: str) -> Optional[str]:
    """Extract a JSON object from a ``` ... ``` fenced code block.

    Uses balanced brace matching within the fence, properly handling
    content that may contain nested braces (like summary text).
    """
    # Find code fence opening
    fence_start = text.find('```')
    while fence_start != -1:
        # Find the end of the opening fence (after ```)
        eol = text.find('\n', fence_start)
        if eol == -1:
            break
        fence_content_start = eol + 1

        # Find matching closing fence
        fence_end = text.find('```', fence_content_start)
        if fence_end == -1:
            break

        candidate = text[fence_content_start:fence_end]
        # Try to extract JSON object from this fence content
        json_str = _extract_json_object(candidate)
        if json_str:
            return json_str

        # Try next fence
        fence_start = text.find('```', fence_end + 1)

    return None


def _extract_json_object(text: str) -> Optional[str]:
    """Extract a JSON object from text using balanced brace matching.

    This is more robust than find-first-{ and find-last-} as it handles
    nested objects and ignores braces in code blocks.
    """
    # Look for the start of a JSON object
    start = text.find('{')
    while start != -1:
        # Try to extract a valid JSON object starting at this position
        depth = 0
        in_string = False
        escape_next = False
        end = -1

        for i in range(start, len(text)):
            char = text[i]

            if escape_next:
                escape_next = False
                continue

            if char == '\\' and in_string:
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string

            if not in_string:
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break

        if end != -1:
            candidate = text[start:end + 1]
            # Quick validation: must start with { and contain valid JSON keys
            if '"' in candidate:  # Must have at least one quoted string (JSON key)
                return candidate

        # Try next occurrence
        start = text.find('{', start + 1)

    return None


def _build_review(data: dict) -> Review:
    """Build a Review object from parsed JSON data."""
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
