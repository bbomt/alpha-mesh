"""The read_file tool — gives the LLM full codebase access with safety guards."""

import os
from urllib.parse import quote

import httpx

READ_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read the full contents of a file in the repository. Use this to understand "
            "types, interfaces, base classes, imported symbols, or existing patterns that the "
            "diff references but does not fully define. Only call this when the file contents "
            "would materially change your verdict on a specific issue. "
            "You must provide a reason for reading the file."
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
                "reason": {
                    "type": "string",
                    "description": (
                        "Brief reason why you are reading this file — e.g. 'checking how "
                        "AuthService is implemented to understand if the diff breaks it'."
                    ),
                },
            },
            "required": ["path", "reason"],
        },
    },
}


class GitHubFileReader:
    """Fetch file contents from GitHub API at a specific commit SHA, with in-memory caching.

    Uses the raw content endpoint so the LLM receives plain text, identical to local reads.
    """

    _GITHUB_API = "https://api.github.com"
    _MAX_CACHE = 256

    def __init__(self, owner: str, repo: str, head_sha: str, token: str) -> None:
        self._owner = owner
        self._repo = repo
        self._ref = head_sha
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw",
            "User-Agent": "review-pull-request/1.0",
        }
        self._client = httpx.Client(headers=self._headers, timeout=30.0)
        self._cache: dict[tuple[str, int], str] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubFileReader":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def read_file(self, path: str, max_lines: int) -> str:
        """Fetch file content at the PR head SHA. Returns content or an error string."""
        cache_key = (path, max_lines)
        if cache_key in self._cache:
            return self._cache[cache_key]

        encoded_path = quote(path, safe="/")
        url = (
            f"{self._GITHUB_API}/repos/{self._owner}/{self._repo}"
            f"/contents/{encoded_path}?ref={self._ref}"
        )

        try:
            response = self._client.get(url)
        except Exception as e:
            return f"Error: failed to fetch file from GitHub: {e}"

        if response.status_code == 404:
            return f"Error: file not found: {path}"

        _rate_limit_phrases = ("rate limit", "secondary rate limit", "you have exceeded")
        if response.status_code == 429 or (
            response.status_code == 403
            and (
                any(p in response.text.lower() for p in _rate_limit_phrases)
                or response.headers.get("x-ratelimit-remaining") == "0"
            )
        ):
            return "Error: GitHub API rate limit exceeded. Try again later."

        if response.status_code == 403:
            # GitHub returns 403 for files >1 MB via the contents API
            try:
                msg = response.json().get("message", "")
            except Exception:
                msg = ""
            if "too large" in msg.lower() or "blob" in msg.lower():
                return (
                    f"Error: file too large to read via GitHub API (>1 MB): {path}. "
                    "Consider reviewing this file locally."
                )
            return f"Error: GitHub API access forbidden for {path}: {msg or response.text[:200]}"

        if response.status_code != 200:
            return f"Error: GitHub API returned {response.status_code} for {path}"

        # Binary detection — same logic as local reads
        try:
            response.content[:8192].decode("utf-8")
        except UnicodeDecodeError:
            return "Error: file appears to be binary and cannot be read as text."

        content = response.text
        lines = content.splitlines(keepends=True)
        if len(lines) > max_lines:
            content = "".join(lines[:max_lines])
            content += (
                f"\n[TRUNCATED — file exceeds {max_lines} lines. "
                f"Showing first {max_lines} lines only.]\n"
            )

        if len(self._cache) >= self._MAX_CACHE:
            self._cache.pop(next(iter(self._cache)))
        self._cache[cache_key] = content
        return content


def execute_read_file(
    path: str,
    repo_root: str,
    max_lines: int,
    github_reader: "GitHubFileReader | None" = None,
) -> str:
    """Read a file with path-traversal guard and optional truncation.

    When github_reader is provided, always reads via GitHub API (remote-always mode).
    Without a reader, reads from the local filesystem.

    Returns the file contents (possibly truncated) or an error string.
    Never raises — errors are returned as strings so the LLM can see them.
    """
    real_repo = os.path.realpath(repo_root)
    target = os.path.realpath(os.path.join(repo_root, path))

    if target != real_repo and not target.startswith(real_repo + os.sep):
        return "Error: access denied — path is outside the repository root."

    if github_reader is not None:
        return github_reader.read_file(path, max_lines)

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
