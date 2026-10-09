"""Split a unified diff into per-file chunks, applying exclusion patterns."""

import re
from dataclasses import dataclass

from review_pull_request.policy import PathPolicy


@dataclass
class FileChunk:
    file_path: str  # new path (old path for a deletion); "" if the header could not be parsed
    diff_text: str
    paths: tuple[str, ...] = ()  # every path the chunk names: old and new sides of a rename/copy


_UNQUOTE_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(path: str) -> str:
    """Undo git's C-style quoting ("caf\\303\\251.py" -> café.py); unquoted paths pass through."""
    if not (len(path) >= 2 and path.startswith('"') and path.endswith('"')):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if len(body[i + 1:i + 4]) == 3 and all(d in "01234567" for d in body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
            out.append(_UNQUOTE_ESCAPES.get(nxt, ord(nxt)))
            i += 2
            continue
        out.extend(ch.encode())
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_side(path: str, side: str) -> str | None:
    """'a/x' -> 'x' for side 'a/'; /dev/null and anything else -> None."""
    path = _unquote(path.rstrip("\n"))
    return path[len(side):] if path.startswith(side) else None


def _paths_from_git_line(rest: str) -> tuple[str, ...]:
    """Paths from 'a/X b/Y' when no ---/+++ or rename lines name them (binary or mode-only changes)."""
    rest = rest.rstrip("\n")
    if rest.startswith('"'):
        end = rest.find('" ', 1)
        while end != -1 and rest[end - 1] == "\\":
            end = rest.find('" ', end + 1)
        if end != -1:
            a, b = _strip_side(rest[:end + 1], "a/"), _strip_side(rest[end + 2:], "b/")
            return tuple(p for p in (a, b) if p)
        return ()
    n = (len(rest) - 5) // 2  # 'a/' + P + ' b/' + P, the usual case where both sides match
    if n > 0 and rest == f"a/{rest[2:2 + n]} b/{rest[2:2 + n]}":
        return (rest[2:2 + n],)
    if rest.startswith("a/") and rest.count(" b/") == 1:  # different sides, split is unambiguous
        a, b = rest.split(" b/")
        return (a[2:], b)
    return ()


def _chunk(header_rest: str, lines: list[str]) -> FileChunk:
    old = new = None
    named: list[str] = []
    for line in lines[1:]:
        if line.startswith("@@"):
            break  # header lines end where the hunks start
        text = line.rstrip("\n")
        if text.startswith("--- "):
            old = _strip_side(text[4:], "a/") or old
        elif text.startswith("+++ "):
            new = _strip_side(text[4:], "b/") or new
        elif text.startswith(("rename from ", "copy from ")):
            old = _unquote(text.split(" ", 2)[2])
        elif text.startswith(("rename to ", "copy to ")):
            new = _unquote(text.split(" ", 2)[2])
    named = [p for p in (old, new) if p]
    if not named:
        named = list(_paths_from_git_line(header_rest))
    paths = tuple(dict.fromkeys(named))
    file_path = new or old or (paths[-1] if paths else "")
    return FileChunk(file_path=file_path, diff_text="".join(lines), paths=paths)


def build_chunks(raw_diff: str, exclusions: list[str]) -> list[FileChunk]:
    """Split a diff into per-file chunks; a chunk is dropped if any path it names is excluded.

    A chunk whose header cannot be parsed has file_path "" (callers must not review it).
    """
    if not raw_diff.strip():
        return []

    sections: list[list[str]] = []
    # Split on "\n" only: splitlines() also breaks on \f, \r, \x85, U+2028..., which would let
    # content inside a file's hunk start a forged "diff --git" section.
    for line in re.split(r"(?<=\n)", raw_diff):
        if not line:
            continue
        if line.startswith("diff --git "):
            sections.append([line])
        elif sections:
            sections[-1].append(line)

    policy = PathPolicy(exclusions)
    chunks = [_chunk(lines[0][len("diff --git "):], lines) for lines in sections]
    return [c for c in chunks
            if not (exclusions and (not c.file_path or any(policy.is_excluded(p) for p in c.paths)))]
