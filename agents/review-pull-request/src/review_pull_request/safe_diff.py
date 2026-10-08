"""SafeDiff — a diff that has already been through the PathPolicy.

Stages accept a SafeDiff, never a raw diff string, so "excluded content reaches the model"
cannot happen by passing the wrong variable. Build one only with SafeDiff.from_raw().
"""

from dataclasses import dataclass

from review_pull_request.chunker import FileChunk, build_chunks
from review_pull_request.policy import PathPolicy


@dataclass(frozen=True)
class SafeDiff:
    chunks: tuple[FileChunk, ...]   # files that may be reviewed
    excluded: tuple[str, ...]       # paths the policy removed, in diff order

    @classmethod
    def from_raw(cls, raw_diff: str, policy: PathPolicy) -> "SafeDiff":
        kept, dropped = [], []
        for c in build_chunks(raw_diff, []):
            # Excluded if any side of a rename/copy is excluded; an unparseable header is never reviewed.
            if not c.file_path:
                dropped.append("(unparseable diff header) " + c.diff_text.split("\n", 1)[0][len("diff --git "):])
            elif any(policy.is_excluded(p) for p in c.paths or (c.file_path,)):
                dropped.append(c.file_path)
            else:
                kept.append(c)
        return cls(chunks=tuple(kept), excluded=tuple(dropped))

    @property
    def text(self) -> str:
        """The diff of the allowed files only (what the overview prompt may contain)."""
        return "".join(c.diff_text for c in self.chunks)

    @property
    def all_excluded(self) -> bool:
        """The diff had files, and the policy removed every one of them."""
        return not self.chunks and bool(self.excluded)
