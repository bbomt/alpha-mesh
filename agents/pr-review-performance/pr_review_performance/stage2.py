"""Stage 2 — Per-file agentic loop with read_file tool.

For each file chunk: sends diff + overview to LLM, handles read_file tool calls,
parses JSON response, returns FileReviewResult.
"""

import json as _json
from dataclasses import dataclass, field

from pr_review_performance.llm import chat, ChatResponse, LLMError
from pr_review_performance.tools import READ_FILE_TOOL_SCHEMA, execute_read_file


PER_FILE_PROMPT = """## PR Context
{overview}

## Task
Review this file's diff for real bugs, security issues, and performance problems.
Do NOT report style issues, formatting, or naming conventions.
Do NOT guess — if you are uncertain whether something is a bug, do not report it.

Before forming a verdict on any issue, if you need to understand a type, interface,
base class, imported symbol, or existing pattern that the diff references but does not
define, call the read_file tool to inspect the relevant file. You may call it multiple
times for different files. Only call it when it would materially change your verdict.

Review all issues in the context of the overall PR changes described above.

Respond with valid JSON only, matching this exact schema:
{{
  "summary": "1-2 sentence overview of this file's changes",
  "issues": [
    {{
      "file": "path/to/file.ts",
      "line": 42,
      "severity": "high",
      "comment": "Description of the issue",
      "suggestion": "How to fix it"
    }}
  ]
}}

Rules:
- "line" must be the actual line number in the file as shown by the '+' column in the
  diff, NOT the diff hunk header line number (the @@ line).
- "severity" must be one of: "high", "medium", "low".
- "suggestion" is required — always provide a concrete fix.
- If no issues are found, return: {{"summary": "...", "issues": []}}
- Do not wrap the JSON in markdown code fences.
- Do not include any text before or after the JSON object.

## Diff
{diff_text}
"""


@dataclass
class Issue:
    file: str
    line: int | None
    severity: str
    comment: str
    suggestion: str | None


@dataclass
class FileReviewResult:
    file_path: str
    summary: str
    issues: list[Issue] = field(default_factory=list)
    tool_calls_made: int = 0


def _build_per_file_prompt(chunk, overview: str) -> str:
    return PER_FILE_PROMPT.format(overview=overview, diff_text=chunk.diff_text)


def _parse_json_response(text: str) -> dict | None:
    """Strip code fences and parse JSON. Returns None on failure."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return _json.loads(text)
    except Exception:
        return None


def run_file_review(chunk, overview: str, config) -> FileReviewResult:
    """Run agentic review for a single file chunk.

    Handles read_file tool calls, retries JSON parse once on failure.
    """
    messages = [
        {"role": "user", "content": _build_per_file_prompt(chunk, overview)}
    ]

    tool_calls_made = 0
    max_iterations = config.max_tool_calls + 1

    for iteration in range(max_iterations):
        try:
            response = chat(
                messages,
                tools=[READ_FILE_TOOL_SCHEMA],
                config=config,
            )
        except LLMError as e:
            print(f"WARNING: LLM call failed: {e}", flush=True)
            return FileReviewResult(
                file_path=chunk.file_path,
                summary="LLM call failed.",
                issues=[],
                tool_calls_made=tool_calls_made,
            )

        if response.tool_calls:
            for tc in response.tool_calls:
                if tc.name == "read_file":
                    path = tc.arguments.get("path", "")
                    result = execute_read_file(
                        path,
                        config.repo_root,
                        config.max_file_lines,
                    )
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": _json.dumps(tc.arguments),
                                },
                            }
                        ],
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                    tool_calls_made += 1

            if tool_calls_made >= config.max_tool_calls:
                return FileReviewResult(
                    file_path=chunk.file_path,
                    summary="Max tool calls reached.",
                    issues=[],
                    tool_calls_made=tool_calls_made,
                )
        else:
            content = response.content or ""
            parsed = _parse_json_response(content)

            if parsed is None and iteration < max_iterations - 1:
                messages.append({
                    "role": "user",
                    "content": (
                        "Your response was not valid JSON. "
                        "Return only valid JSON matching the schema described earlier. "
                        "No markdown, no explanation."
                    ),
                })
                continue

            if parsed is None:
                return FileReviewResult(
                    file_path=chunk.file_path,
                    summary="Could not parse model response.",
                    issues=[],
                    tool_calls_made=tool_calls_made,
                )

            issues = []
            for iss in parsed.get("issues", []):
                issues.append(Issue(
                    file=iss.get("file", chunk.file_path),
                    line=iss.get("line"),
                    severity=iss.get("severity", "medium"),
                    comment=iss.get("comment", ""),
                    suggestion=iss.get("suggestion"),
                ))

            return FileReviewResult(
                file_path=chunk.file_path,
                summary=parsed.get("summary", ""),
                issues=issues,
                tool_calls_made=tool_calls_made,
            )

    return FileReviewResult(
        file_path=chunk.file_path,
        summary="Max iterations reached.",
        issues=[],
        tool_calls_made=tool_calls_made,
    )
