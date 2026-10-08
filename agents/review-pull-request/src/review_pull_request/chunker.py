"""Split a unified diff into per-file chunks, applying exclusion patterns."""

from dataclasses import dataclass
import fnmatch
import os


@dataclass
class FileChunk:
    file_path: str
    diff_text: str


def _matches_exclusion(file_path: str, exclusions: list[str]) -> bool:
    """Check if file_path matches any exclusion pattern.

    Handles both literal fnmatch patterns (package-lock.json) and
    directory wildcard patterns (*/dist/*).
    For */inner/* style patterns: matches files where 'inner' appears
    as a path component (e.g. dist/bundle.js matches */dist/* because dist is a path component).
    """
    # Case-insensitive on purpose: ".ENV" must not slip past "**/.env" (over-matching is fail-safe).
    file_path = file_path.lower()
    for pattern in exclusions:
        pattern = pattern.lower()
        if fnmatch.fnmatchcase(file_path, pattern):
            return True
        # "**/name" also matches "name" at the repo root (zero directories).
        if pattern.startswith("**/") and fnmatch.fnmatchcase(file_path, pattern[3:]):
            return True
        if pattern.startswith("*") and pattern.endswith("*") and len(pattern) > 2:
            inner = pattern[1:-1].strip("/")
            parts = file_path.split(os.sep)
            if inner in parts:
                return True
    return False


def is_excluded(file_path: str, exclusions: list[str]) -> bool:
    """True if file_path matches an exclusion pattern (also used to guard read_file)."""
    return _matches_exclusion(file_path, exclusions)


def chunk_exists(file_path: str, exclusions: list[str]) -> tuple[bool, bool]:
    if _matches_exclusion(file_path, exclusions):
        return True, True
    return False, False


def build_chunks(raw_diff: str, exclusions: list[str]) -> list[FileChunk]:
    if not raw_diff.strip():
        return []

    chunks: list[FileChunk] = []
    current_path = ""
    current_lines: list[str] = []

    for line in raw_diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path:
                diff_text = "".join(current_lines)
                skipped, excluded = chunk_exists(current_path, exclusions)
                if not excluded:
                    chunks.append(FileChunk(file_path=current_path, diff_text=diff_text))
            parts = line.split(" ", 3)
            if len(parts) >= 4:
                b_full = parts[3].rstrip("\n")
                a_full = parts[2]
                if b_full.startswith("b/"):
                    current_path = b_full[2:].strip()
                elif a_full.startswith("a/"):
                    current_path = a_full[2:].strip()
            current_lines = [line]
        else:
            if current_path:
                current_lines.append(line)

    if current_path:
        diff_text = "".join(current_lines)
        skipped, excluded = chunk_exists(current_path, exclusions)
        if not excluded:
            chunks.append(FileChunk(file_path=current_path, diff_text=diff_text))

    return chunks
