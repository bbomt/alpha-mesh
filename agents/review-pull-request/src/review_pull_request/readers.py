"""File readers behind the read_file tool.

LocalFileReader reads a checkout; GitHubFileReader (tools.py) reads over the API. Both are
wrapped in PolicyFileReader (Decorator), which refuses excluded paths before any read happens.
"""

import codecs
import itertools
import os

from review_pull_request.policy import PathPolicy, normalise


class ReadError(str):
    """A failed read, as text for the model. Content that merely starts with 'Error:' is not one."""


MAX_READ_BYTES = 1024 * 1024  # a file of a few huge lines is still bounded


class LocalFileReader:
    """Reads files under a local repo root, with a traversal guard and truncation."""

    def __init__(self, repo_root: str) -> None:
        self._root = os.path.realpath(repo_root)

    def canonical_path(self, path: str) -> str | None:
        """Repo-relative path after resolving symlinks, or None if it escapes the root."""
        target = os.path.realpath(os.path.join(self._root, path))
        if target != self._root and not target.startswith(self._root + os.sep):
            return None
        return os.path.relpath(target, self._root).replace(os.sep, "/")

    def read_file(self, path: str, max_lines: int) -> str:
        real = self.canonical_path(path)
        if real is None:
            return ReadError("Error: access denied — path is outside the repository root.")
        target = os.path.join(self._root, real)

        if not os.path.isfile(target):
            return ReadError(f"Error: file not found: {path}")

        try:
            with open(target, "rb") as f:
                raw = f.read(8192)
            codecs.getincrementaldecoder("utf-8")().decode(raw, final=False)  # a char may straddle the cut
        except UnicodeDecodeError:
            return ReadError("Error: file appears to be binary and cannot be read as text.")
        except (OSError, ValueError) as e:
            return ReadError(f"Error: could not read file: {e}")

        try:
            with open(target, "rb") as f:
                raw = f.read(MAX_READ_BYTES + 1)  # never load more than needed
            text = codecs.getincrementaldecoder("utf-8")(errors="replace").decode(raw[:MAX_READ_BYTES], final=False)
        except Exception as e:
            return ReadError(f"Error: could not read file: {e}")

        lines = list(itertools.islice(text.splitlines(keepends=True), max_lines + 1))
        if len(raw) > MAX_READ_BYTES and len(lines) <= max_lines:
            return "".join(lines) + f"\n[TRUNCATED — file exceeds {MAX_READ_BYTES} bytes.]\n"
        if len(lines) > max_lines:
            truncated = "".join(lines[:max_lines])
            truncated += f"\n[TRUNCATED — file exceeds {max_lines} lines. Showing first {max_lines} lines only.]\n"
            return truncated
        return "".join(lines)


class PolicyFileReader:
    """Decorator: same read_file() interface, but excluded or out-of-root paths are refused."""

    def __init__(self, inner, policy: PathPolicy) -> None:
        self._inner = inner
        self._policy = policy

    def read_file(self, path: str, max_lines: int) -> str:
        if not isinstance(path, str) or not path or "\x00" in path:  # model-supplied, so untrusted
            return ReadError(f"Error: invalid path: {path!r}")
        clean = normalise(path)
        if clean == ".." or clean.startswith("../"):
            return ReadError("Error: access denied — path is outside the repository root.")
        if self._policy.is_excluded(clean):
            return self._refused(path)
        # A symlink (or case alias) can resolve to an excluded file: check the real target too.
        canonical = getattr(self._inner, "canonical_path", None)
        if canonical is not None:
            real = canonical(clean)
            if real is not None and self._policy.is_excluded(real):
                return self._refused(path)
        return self._inner.read_file(clean, max_lines)

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()

    @staticmethod
    def _refused(path: str) -> ReadError:
        return ReadError(f"Error: path is excluded from review by the exclusion patterns: {path}")
