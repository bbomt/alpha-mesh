"""Stage 2 — Per-file agentic loop with read_file tool.

For each file chunk: sends diff + overview to LLM, handles read_file tool calls,
parses JSON response, returns FileReviewResult.
"""

import json as _json
import sys
from dataclasses import dataclass, field

from review_pull_request.policy import normalise
from review_pull_request.readers import NotFound, ReadError
from review_pull_request.tools import READ_FILE_TOOL_SCHEMA


PER_FILE_PROMPT = """## Task
Review this file's diff for real bugs, security issues, performance problems, and architectural concerns. Do NOT report style issues, formatting, or naming conventions. Do NOT guess — if you are uncertain whether something is a bug, do not report it.

## PR Context
{overview}

## Tool use
Before forming a verdict on any issue, if you need to understand a type, interface, base class, imported symbol, or existing pattern that the diff references but does not define, call the read_file tool to inspect the relevant file. You may call it multiple times for different files. Only call it when it would materially change your verdict. You MUST provide a "reason" argument explaining why you need to read the file — e.g. "checking how AuthService is implemented to verify the diff does not break the login flow".

## Mandatory checks
You must explicitly audit the diff for ALL of the following. Do not skip a category because it seems unlikely — check each one.

### Architectural & scalability
- Large bundles or modules loaded eagerly that could be code-split
- Synchronous operations blocking the event loop (heavy loops, deep recursion, large serialisations)
- Unbounded data structures or caches that grow without limit
- Missing pagination or streaming where a list endpoint returns all records
- Tight coupling between layers (e.g. business logic embedded directly in UI components)
- Missing error boundaries or graceful degradation

### React-specific bugs
- **Prop spread order:** if library/third-party props are spread AFTER explicit local event handlers (e.g. `<Input onChange={{handleChange}} ...{{{{...props}}}} />`), the spread silently overwrites the handler. Flag as HIGH severity if present; explicit props must come after the spread, or destructure first.
- **State wipe on re-render:** `useEffect` hooks whose dependency array contains an object or array reference that changes on every parent render (e.g. `useEffect(() => {{ setState(x) }}, [props.obj])`) will clobber user-edited local state. Verify object identity, not just shape.
- **Async race conditions in handlers:** form submission and step-navigation handlers (e.g. `handleNextClick`) that call validation or mutation APIs without `await` allow fast-clicking past in-flight operations. Check that promises are awaited or the UI is locked during the request.
- **Stale closures:** missing or incorrect dependency arrays in `useEffect` / `useCallback` / `useMemo`.
- **Derived state lifecycle:** state initialised from props with no mechanism to stay in sync (neither controlled nor properly reset).

### Security
- **Stored XSS via href:** user-controlled data passed directly into an `href` attribute without validation enables `javascript:` protocol injection. Treat any unvalidated variable in `href` as HIGH severity.
- Unescaped user input rendered as HTML (`dangerouslySetInnerHTML` or equivalent).
- Secrets, tokens, or credentials in client-side code or logs.

### Data integrity
- Runtime metadata added to core data models (e.g. a `.platform` tag appended client-side) that may break backend schema validation, database constraints, or API type contracts.
- Missing input validation or missing server-side re-validation of client-supplied values.

## Context
Review all issues in the context of the overall PR changes described above.

## Output format
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
- "line" must be the actual line number shown by the '+' column in the diff, NOT the @@ hunk header number.
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
    incomplete: bool = False  # the file could not be fully reviewed (do not report it as clean)


_SEVERITY_ALIASES = {
    "error": "high", "critical": "high", "blocker": "high", "major": "high",
    "warning": "medium", "moderate": "medium",
    "minor": "low", "info": "low", "nit": "low", "suggestion": "low",
}


def _normalise_severity(value) -> str:
    """Map whatever the model emitted onto high/medium/low so no finding is dropped."""
    sev = str(value or "medium").strip().lower()
    sev = _SEVERITY_ALIASES.get(sev, sev)
    return sev if sev in ("high", "medium", "low") else "medium"  # unknown: do not understate


def _build_per_file_prompt(chunk, overview: str) -> str:
    return PER_FILE_PROMPT.format(overview=overview, diff_text=chunk.diff_text)


def _parse_json_response(text: str) -> dict | None:
    """Extract and parse the JSON object in a model reply (bare, fenced, or wrapped in prose).

    Returns None on failure.
    """
    text = text.strip()
    candidates = [text]
    # Fenced blocks, last first: the answer comes last, after any echoed schema example.
    for fenced in reversed(text.split("```")[1::2]):
        candidates.append(fenced[4:] if fenced.startswith("json") else fenced)
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for candidate in candidates:
        try:
            parsed = _json.loads(candidate.strip())
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


DEFAULT_READ_CACHE_MAX_BYTES = 50 * 1024 * 1024

READ_LIMIT_NOTE = (
    "You have reached the file-read limit: no more reads are available. "
    "Return your JSON answer now, based on what you have read."
)

ALREADY_READ_NOTE = (
    "You already read this file earlier in this conversation; its content is above. "
    "Do not request it again. Continue the review or return your JSON answer."
)

NOT_FOUND_AGAIN_NOTE = (
    "You already requested this path and it does not exist. Do not request it again. "
    "Try a different path or return your JSON answer."
)


def _with_hint(result: NotFound, suggestions: list[str]) -> NotFound:
    if not suggestions:
        return result
    return NotFound(f"{result}\nFiles with the same name in this PR: {', '.join(suggestions)}")


def run_file_review(chunk, overview: str, session) -> FileReviewResult:
    """Run agentic review for a single file chunk.

    Model calls go through session.review_llm and file reads through session.reader, which
    already enforce the exclusion policy. Handles read_file tool calls, retries JSON parse once.
    LLMError propagates: an unusable model API is fatal.
    """
    config = session.config
    messages = [
        {"role": "user", "content": _build_per_file_prompt(chunk, overview)}
    ]

    tool_calls_made = 0
    max_iterations = config.max_tool_calls + 3  # reads, then the answer, then one JSON retry
    tools = [READ_FILE_TOOL_SCHEMA]
    read_cache = getattr(config, "read_cache", None)
    paths_read: set[str] = set()  # paths already shown to the model in this review
    paths_missing: set[str] = set()  # paths that came back "not found" in this review

    for iteration in range(max_iterations):
        response = session.review_llm.chat(messages, tools=tools)

        if response.tool_calls and tools is None:
            # Past the read limit the model was told to answer; asking again means no answer.
            return FileReviewResult(
                file_path=chunk.file_path,
                summary="Max tool calls reached.",
                issues=[],
                tool_calls_made=tool_calls_made,
                incomplete=True,
            )
        if response.tool_calls:
            for tc in response.tool_calls:
                if tc.name == "read_file":
                    path = tc.arguments.get("path", "")
                    reason = tc.arguments.get("reason", "")
                    print(f"  → read_file: {path}  (while reviewing {chunk.file_path})", file=sys.stderr, flush=True)
                    print(f"     reason: {reason}", file=sys.stderr, flush=True)
                    key = normalise(path)  # one spelling per file, for dedup and the cache
                    if key in paths_read:
                        result = ALREADY_READ_NOTE
                    elif key in paths_missing:
                        result = NotFound(NOT_FOUND_AGAIN_NOTE)
                        print("     (not found, repeated — not re-read)", file=sys.stderr, flush=True)
                    elif read_cache is not None and key in read_cache:
                        result = read_cache[key]
                        print("     (cached)", file=sys.stderr, flush=True)
                    else:
                        result = session.reader.read_file(path, config.max_file_lines)
                        if read_cache is not None and not isinstance(result, ReadError):
                            cap = getattr(config, "read_cache_max_bytes", DEFAULT_READ_CACHE_MAX_BYTES)
                            size = len(result.encode())
                            if getattr(config, "read_cache_bytes", 0) + size <= cap:
                                read_cache[key] = result
                                config.read_cache_bytes = getattr(config, "read_cache_bytes", 0) + size
                        if isinstance(result, NotFound):
                            paths_missing.add(key)  # retrying cannot help, unlike a transient error
                            suggestions = session.suggest_paths(path)
                            result = _with_hint(result, suggestions)
                            hint = f"; suggested: {', '.join(suggestions)}" if suggestions else ""
                            print(f"     (not found{hint})", file=sys.stderr, flush=True)
                        elif isinstance(result, ReadError):
                            print(f"     (failed: {result})", file=sys.stderr, flush=True)
                    if not isinstance(result, ReadError):
                        paths_read.add(key)  # failed reads stay retryable
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
                # Keep what the model has learned: no more reads, ask for the answer now.
                tools = None
                messages.append({"role": "user", "content": READ_LIMIT_NOTE})
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
                    incomplete=True,
                )

            issues = []
            raw_issues = parsed.get("issues")
            malformed = not isinstance(raw_issues, list) or any(not isinstance(i, dict) for i in raw_issues)
            for iss in raw_issues if isinstance(raw_issues, list) else []:
                if not isinstance(iss, dict):
                    continue
                issues.append(Issue(
                    file=iss.get("file", chunk.file_path),
                    line=iss.get("line"),
                    severity=_normalise_severity(iss.get("severity")),
                    comment=iss.get("comment", ""),
                    suggestion=iss.get("suggestion"),
                ))

            return FileReviewResult(
                file_path=chunk.file_path,
                summary=parsed.get("summary", ""),
                issues=issues,
                tool_calls_made=tool_calls_made,
                incomplete=malformed,  # a garbled issues payload must not read as "clean"
            )

    return FileReviewResult(
        file_path=chunk.file_path,
        summary="Max iterations reached.",
        issues=[],
        tool_calls_made=tool_calls_made,
        incomplete=True,
    )
