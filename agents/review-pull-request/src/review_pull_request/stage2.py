"""Stage 2 — Per-file agentic loop with read_file tool.

For each file chunk: sends diff + overview to LLM, handles read_file tool calls,
parses JSON response, returns FileReviewResult.
"""

import json as _json
import posixpath
import sys
from dataclasses import dataclass, field

from review_pull_request.policy import normalise
from review_pull_request.prompts import load_prompt
from review_pull_request.readers import NotFound, ReadError
from review_pull_request.tools import READ_FILE_TOOL_SCHEMA


PER_FILE_PROMPT = load_prompt("per_file.md")


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
    files_read: list[str] = field(default_factory=list)  # paths whose content the model was shown


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


def _folder_name(folder: str) -> str:
    return f"`{folder}/`" if folder else "the repository root"


def _build_per_file_prompt(chunk, overview: str, session) -> str:
    folder = posixpath.dirname(chunk.file_path)
    entries = session.folder_listing(folder)
    listing = f"Files in the folder of the file under review ({_folder_name(folder)}): {', '.join(entries)}" if entries else ""
    return PER_FILE_PROMPT.format(overview=overview, diff_text=chunk.diff_text, folder_listing=listing)


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


UNKNOWN_TOOL_NOTE = "Unknown tool: only read_file is available."

BAD_PATH_NOTE = "Error: read_file needs a non-empty string 'path'."


def _to_issue(iss, default_file: str) -> Issue | None:
    """An Issue from one model-supplied finding, or None if its fields have the wrong types."""
    if not isinstance(iss, dict):
        return None
    file = iss.get("file", default_file)
    line = iss.get("line")
    comment = iss.get("comment", "")
    suggestion = iss.get("suggestion")
    if isinstance(line, str) and line.strip().isdigit():
        line = int(line)
    if (not isinstance(file, str) or not file
            or not (line is None or (isinstance(line, int) and not isinstance(line, bool)))
            or not isinstance(comment, str)
            or not (suggestion is None or isinstance(suggestion, str))):
        return None
    return Issue(file=file, line=line, severity=_normalise_severity(iss.get("severity")),
                 comment=comment, suggestion=suggestion)


def _with_hints(result: NotFound, suggestions: list[str], folder: str, entries: list[str]) -> NotFound:
    lines = [str(result)]
    if suggestions:
        lines.append(f"Files with the same name: {', '.join(suggestions)}")
    if entries:
        lines.append(f"Nearest existing folder {_folder_name(folder)} contains: {', '.join(entries)}")
    return NotFound("\n".join(lines))


def run_file_review(chunk, overview: str, session) -> FileReviewResult:
    """Run agentic review for a single file chunk.

    Model calls go through session.review_llm and file reads through session.reader, which
    already enforce the exclusion policy. Handles read_file tool calls, retries JSON parse once.
    LLMError propagates: an unusable model API is fatal.

    Only misses the model did not recover from reach the report: a miss is recovered if, by the
    end of this review, one of its suggested paths was read or is the file under review.
    """
    paths_read: set[str] = set()  # paths already shown to the model in this review
    misses: dict[str, list[str]] = {}  # path that came back "not found" -> suggested paths
    with session.trace.file_review(chunk.file_path) as span:  # written only if the review ends
        result = _run_file_review(chunk, overview, session, paths_read, misses)
        result.files_read = sorted(paths_read)
        span.output = {"files_read": result.files_read, "incomplete": result.incomplete,
                       "tool_calls_made": result.tool_calls_made}
    seen = paths_read | {normalise(chunk.file_path)}
    for path, suggestions in misses.items():
        if not seen.intersection(suggestions):
            session.note_missing(path, chunk.file_path)
    return result


def _run_file_review(chunk, overview: str, session, paths_read: set[str],
                     misses: dict[str, list[str]]) -> FileReviewResult:
    config = session.config
    messages = [
        {"role": "user", "content": _build_per_file_prompt(chunk, overview, session)}
    ]

    tool_calls_made = 0
    max_iterations = config.max_tool_calls + 3  # reads, then the answer, then one JSON retry
    tools = [READ_FILE_TOOL_SCHEMA]
    read_cache = getattr(config, "read_cache", None)

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
                path = tc.arguments.get("path", "")
                if tc.name != "read_file":
                    print(f"  → unknown tool {tc.name!r} refused  (while reviewing {chunk.file_path})",
                          file=sys.stderr, flush=True)
                    result = UNKNOWN_TOOL_NOTE  # every tool call gets an answer
                elif not isinstance(path, str) or not path.strip():
                    print(f"  → read_file without a usable path refused  (while reviewing {chunk.file_path})",
                          file=sys.stderr, flush=True)
                    result = ReadError(BAD_PATH_NOTE)
                else:
                    reason = tc.arguments.get("reason", "")
                    print(f"  → read_file: {path}  (while reviewing {chunk.file_path})", file=sys.stderr, flush=True)
                    print(f"     reason: {reason}", file=sys.stderr, flush=True)
                    key = normalise(path)  # one spelling per file, for dedup and the cache
                    if key in paths_read:
                        result = ALREADY_READ_NOTE
                    elif key in misses:
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
                            suggestions = session.suggest_paths(path)
                            misses[key] = suggestions  # retrying cannot help, unlike a transient error
                            folder = session.nearest_folder(path)
                            result = _with_hints(result, suggestions, folder, session.folder_listing(folder))
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
                                "name": tc.name,
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
                issue = _to_issue(iss, chunk.file_path)
                if issue is None:
                    malformed = True  # a badly typed finding is dropped, so the file is not clean
                    continue
                issues.append(issue)

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
