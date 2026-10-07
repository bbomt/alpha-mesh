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
    for pattern in exclusions:
        if fnmatch.fnmatch(file_path, pattern):
            return True
        # "**/name" also matches "name" at the repo root (zero directories).
        if pattern.startswith("**/") and fnmatch.fnmatch(file_path, pattern[3:]):
            return True
        if pattern.startswith("*") and pattern.endswith("*") and len(pattern) > 2:
            inner = pattern[1:-1].strip("/")
            parts = file_path.split(os.sep)
            if inner in parts:
                return True
    return False


def chunk_exists(file_path: str, exclusions: list[str]) -> tuple[bool, bool]:
    if _matches_exclusion(file_path, exclusions):
        return True, True
    return False, False


def _add_chunk(chunks, skipped, path, diff_text, exclusions) -> None:
    _, excluded = chunk_exists(path, exclusions)
    if excluded:
        return
    reason = _skip_reason(diff_text)
    if reason:
        if skipped is not None:
            skipped.append((path, reason))
        return
    chunks.append(FileChunk(file_path=path, diff_text=diff_text))


def _skip_reason(diff_text: str) -> str | None:
    """Why a file diff has no code worth a per-file review, or None if it does."""
    header = diff_text.split("\n@@", 1)[0]
    if "\ndeleted file mode" in header:
        return "deleted"
    if "\nsimilarity index 100%" in header and "\n@@" not in diff_text:
        return "renamed without changes"
    return None


def build_chunks(
    raw_diff: str,
    exclusions: list[str],
    skipped: list[tuple[str, str]] | None = None,
) -> list[FileChunk]:
    """Split a diff into per-file chunks.

    Files matching an exclusion pattern are dropped silently. Deleted files and
    pure renames carry no code to review (the overview pass still sees them);
    they are dropped and, if `skipped` is given, recorded there as (path, reason).
    """
    if not raw_diff.strip():
        return []

    chunks: list[FileChunk] = []
    current_path = ""
    current_lines: list[str] = []

    for line in raw_diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path:
                diff_text = "".join(current_lines)
                _add_chunk(chunks, skipped, current_path, diff_text, exclusions)
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
        _add_chunk(chunks, skipped, current_path, diff_text, exclusions)

    return chunks
