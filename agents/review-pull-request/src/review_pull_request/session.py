"""ReviewSession — Facade that wires one review run together.

It builds the PathPolicy once and hands the stages only wrapped collaborators:
  * review_llm                : model client wrapped in GuardedLLM (secrets redacted)
  * reader                    : file reader wrapped in PolicyFileReader (excluded paths refused)
  * safe_diff(raw)            : a SafeDiff (excluded files removed)
  * suggest_paths(path)       : "did you mean" paths for a missing file, from the kept diff only
Stages receive the session, not the raw pieces, so they have no way around the policy.
"""

import posixpath

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
        self._known_paths: tuple[str, ...] = ()  # files in the last SafeDiff, for not-found hints

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
        safe = SafeDiff.from_raw(raw_diff, self.policy)
        self._known_paths = tuple(c.file_path for c in safe.chunks)
        return safe

    def suggest_paths(self, path: str, limit: int = 3) -> list[str]:
        """Files in this PR with the same name as a path that was not found (never excluded ones)."""
        missing = normalise(path)
        name = posixpath.basename(missing).lower()
        return [p for p in dict.fromkeys(self._known_paths)
                if posixpath.basename(p).lower() == name and normalise(p) != missing
                and not self.policy.is_excluded(p)][:limit]
