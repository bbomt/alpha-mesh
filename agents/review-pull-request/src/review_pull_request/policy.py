"""PathPolicy — the single place where repo paths are compared with exclusion patterns.

Everything that decides "may this file be reviewed / shown to the model?" asks a PathPolicy.
It normalises the path first (backslashes, leading '/', './', '..') and compares
case-insensitively, so ".ENV", "./.env" and "svc/../.env" are all the same file as ".env".
Over-matching is the safe direction: an excluded file is simply listed in the report.
"""

import fnmatch
import posixpath
from collections.abc import Iterable


def normalise(path: str) -> str:
    """Canonical spelling of a repo-relative path ('/'-separated, no './', no '..' inside)."""
    return posixpath.normpath(path.replace("\\", "/").lstrip("/"))


class PathPolicy:
    """Immutable set of exclusion patterns with one question: is_excluded(path)?"""

    def __init__(self, patterns: Iterable[str] = ()):
        self._patterns = tuple(dict.fromkeys(patterns))

    @property
    def patterns(self) -> tuple[str, ...]:
        return self._patterns

    def is_excluded(self, path: str) -> bool:
        target = normalise(path).lower()
        for raw in self._patterns:
            pattern = raw.lower()
            if fnmatch.fnmatchcase(target, pattern):
                return True
            # "**/name" also matches "name" at the repo root (zero directories).
            if pattern.startswith("**/") and fnmatch.fnmatchcase(target, pattern[3:]):
                return True
            # "*/inner/*": 'inner' as any path component.
            if pattern.startswith("*") and pattern.endswith("*") and len(pattern) > 2:
                if pattern[1:-1].strip("/") in target.split("/"):
                    return True
        return False

    def __or__(self, other: "PathPolicy") -> "PathPolicy":
        return PathPolicy(self._patterns + other._patterns)

    def __repr__(self) -> str:
        return f"PathPolicy({list(self._patterns)!r})"
