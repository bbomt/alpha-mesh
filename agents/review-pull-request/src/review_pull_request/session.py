"""ReviewSession — Facade that wires one review run together.

It builds the PathPolicy once and hands the stages only wrapped collaborators:
  * review_llm                : model client wrapped in GuardedLLM (secrets redacted)
  * reader                    : file reader wrapped in PolicyFileReader (excluded paths refused)
  * safe_diff(raw)            : a SafeDiff (excluded files removed)
  * folder_listing(folder)    : a folder's entries, from the policy-filtered repo tree
  * nearest_folder(path)      : the closest folder of a path that exists in that tree
  * suggest_paths(path)       : files anywhere in that tree with the same name as a missing path
Stages receive the session, not the raw pieces, so they have no way around the policy.
"""

import posixpath
import sys

from review_pull_request.guard import GuardedLLM, Redactor
from review_pull_request.llm import HttpLLM
from review_pull_request.policy import PathPolicy, normalise
from review_pull_request.readers import PolicyFileReader
from review_pull_request.safe_diff import SafeDiff


class ReviewSession:
    def __init__(self, config, policy, review_llm, reader) -> None:
        self.config = config
        self.policy = policy
        self.review_llm = review_llm
        self.reader = reader
        self._files: list[str] | None = None  # repo tree minus excluded files, loaded once

    @classmethod
    def create(cls, config, inner_reader, llm_factory=None, secrets=()) -> "ReviewSession":
        factory = llm_factory or HttpLLM
        policy = PathPolicy(config.exclusions)
        redactor = Redactor([config.api_token, *secrets])
        return cls(
            config=config,
            policy=policy,
            review_llm=GuardedLLM(factory(config, config.model), redactor),
            reader=PolicyFileReader(inner_reader, policy),
        )

    def safe_diff(self, raw_diff: str) -> SafeDiff:
        return SafeDiff.from_raw(raw_diff, self.policy)

    def _tree(self) -> list[str]:
        # Through the PolicyFileReader, so excluded files are already gone; folders are derived
        # only from the files that remain, so a folder of excluded files never shows up either.
        if self._files is None:
            files = self.reader.list_files()
            if files is None:
                print("  (file listing unavailable: reviews continue without folder hints)",
                      file=sys.stderr, flush=True)
            self._files = files or []
        return self._files

    def folder_listing(self, folder: str, limit: int = 50) -> list[str]:
        """Names directly in a folder ('' is the root): files, then sub-folders ending in '/'."""
        folder = normalise(folder)
        prefix = "" if folder == "." else f"{folder}/"
        entries = set()
        for p in self._tree():
            if p.startswith(prefix):
                name, sep, _ = p[len(prefix):].partition("/")
                entries.add(name + sep)  # "sub/" for a sub-folder
        names = sorted(entries, key=lambda e: (e.endswith("/"), e))
        return names if len(names) <= limit else names[:limit] + [f"... ({len(names) - limit} more)"]

    def nearest_folder(self, path: str) -> str:
        """The deepest parent folder of path that has files in the tree ('' is the root)."""
        folder = posixpath.dirname(normalise(path))
        while folder and not any(p.startswith(f"{folder}/") for p in self._tree()):
            folder = posixpath.dirname(folder)
        return folder

    def suggest_paths(self, path: str, limit: int = 3) -> list[str]:
        """Files anywhere in the repo with the same name as a path that was not found."""
        missing = normalise(path)
        name = posixpath.basename(missing).lower()
        return [p for p in self._tree()
                if posixpath.basename(p).lower() == name and p != missing][:limit]
