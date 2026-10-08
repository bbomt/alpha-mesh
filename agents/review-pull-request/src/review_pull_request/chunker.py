"""Split a unified diff into per-file chunks, applying exclusion patterns."""

from dataclasses import dataclass

from review_pull_request.policy import PathPolicy


@dataclass
class FileChunk:
    file_path: str
    diff_text: str


def chunk_exists(file_path: str, exclusions: list[str]) -> tuple[bool, bool]:
    # The comparison itself lives in PathPolicy.
    if PathPolicy(exclusions).is_excluded(file_path):
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
