"""The read_file tool — gives the LLM full codebase access with safety guards."""

import os

READ_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read the full contents of a file in the repository. Use this to understand "
            "types, interfaces, base classes, imported symbols, or existing patterns that the "
            "diff references but does not fully define. Only call this when the file contents "
            "would materially change your verdict on a specific issue."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path to the file relative to the repository root. "
                        "Example: 'src/auth/token.ts'"
                    ),
                },
            },
            "required": ["path"],
        },
    },
}


def execute_read_file(path: str, repo_root: str, max_lines: int) -> str:
    """Read a file with path-traversal guard and optional truncation.

    Returns the file contents (possibly truncated) or an error string.
    Never raises — errors are returned as strings so the LLM can see them.
    """
    real_repo = os.path.realpath(repo_root)
    target = os.path.realpath(os.path.join(repo_root, path))

    if not target.startswith(real_repo):
        return "Error: access denied — path is outside the repository root."

    if not os.path.isfile(target):
        return f"Error: file not found: {path}"

    try:
        with open(target, "rb") as f:
            raw = f.read(8192)
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "Error: file appears to be binary and cannot be read as text."

    try:
        with open(target, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"Error: could not read file: {e}"

    if len(lines) > max_lines:
        truncated = "".join(lines[:max_lines])
        truncated += f"\n[TRUNCATED — file exceeds {max_lines} lines. Showing first {max_lines} lines only.]\n"
        return truncated

    return "".join(lines)
