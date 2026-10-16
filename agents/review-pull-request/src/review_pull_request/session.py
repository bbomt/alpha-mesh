"""ReviewSession — Facade that wires one review run together.

It builds the PathPolicy once and hands the stages only wrapped collaborators:
  * review_llm                : model client wrapped in GuardedLLM (secrets redacted)
  * reader                    : file reader wrapped in PolicyFileReader (excluded paths refused)
  * safe_diff(raw)            : a SafeDiff (excluded files removed)
  * folder_listing(folder)    : a folder's entries, from the policy-filtered repo tree
  * nearest_folder(path)      : the closest folder of a path that exists in that tree
  * suggest_paths(path)       : files anywhere in that tree with the same name as a missing path
  * note_missing(path, file)  : records a missing path the model did not recover from (for the report)
Stages receive the session, not the raw pieces, so they have no way around the policy.
"""

import posixpath
import sys
from urllib.parse import urlparse

from langchain_openai import ChatOpenAI

from review_pull_request.guard import GuardedLLM, Redactor
from review_pull_request.llm import DRY_RUN_MODEL, DryRunChatModel, LangChainLLM
from review_pull_request.policy import PathPolicy, normalise
from review_pull_request.readers import PolicyFileReader
from review_pull_request.safe_diff import SafeDiff


OPENROUTER_HOST = "openrouter.ai"


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:  # e.g. unbalanced IPv6 brackets
        return ""


def make_chat_model(config, model: str):
    """The chat model for CODE_REVIEW_URL: ChatOpenAI for every OpenAI-compatible endpoint.

    Names are `{provider}/{modelname}`: openrouter.ai gets the full name, other hosts get it
    without the `provider/` prefix. Key and URL come from config, never a provider env var.
    """
    if model == DRY_RUN_MODEL:
        return DryRunChatModel()
    if _host(config.api_url) != OPENROUTER_HOST and "/" in model:
        model = model.split("/", 1)[1]
    base_url = config.api_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    return ChatOpenAI(model=model, api_key=config.api_token, base_url=base_url,
                      temperature=0.2, max_retries=2, timeout=120)


class ReviewSession:
    def __init__(self, config, policy, review_llm, reader) -> None:
        self.config = config
        self.policy = policy
        self.review_llm = review_llm
        self.reader = reader
        self._files: list[str] | None = None  # repo tree minus excluded files, loaded once
        self._listed = False
        self.missing_paths: dict[str, list[str]] = {}  # missing path -> files whose review asked for it

    @classmethod
    def create(cls, config, inner_reader, chat_model_factory=None, secrets=()) -> "ReviewSession":
        factory = chat_model_factory or make_chat_model
        policy = PathPolicy(config.exclusions)
        redactor = Redactor([config.api_token, *secrets])
        llm = LangChainLLM(factory(config, config.model), config.model, _host(config.api_url) or "(invalid URL)")
        return cls(
            config=config,
            policy=policy,
            review_llm=GuardedLLM(llm, redactor),
            reader=PolicyFileReader(inner_reader, policy),
        )

    def safe_diff(self, raw_diff: str) -> SafeDiff:
        return SafeDiff.from_raw(raw_diff, self.policy)

    def _tree(self) -> list[str]:
        # Through the PolicyFileReader, so excluded files are already gone; folders are derived
        # only from the files that remain, so a folder of excluded files never shows up either.
        # Fetched once per run, even on failure: a truncated tree stays truncated, and the
        # listing is only an aid (the report notes when it was missing).
        if not self._listed:
            self._listed = True
            self._files = self.reader.list_files()
            if self._files is None:
                print("  (file listing unavailable: reviews continue without folder hints)",
                      file=sys.stderr, flush=True)
        return self._files or []

    @property
    def listing_failed(self) -> bool:
        """True if the repo file listing was fetched and could not be had."""
        return self._listed and self._files is None

    def symlinks(self) -> list[str]:
        """Symlinks in the repo (excluded names dropped), for the report to warn about."""
        self._tree()
        return self.reader.symlinks

    def note_missing(self, path: str, reviewing: str) -> None:
        files = self.missing_paths.setdefault(normalise(path), [])
        if reviewing not in files:
            files.append(reviewing)

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
        folder, tree = posixpath.dirname(normalise(path)), self._tree()
        while folder and not any(p.startswith(f"{folder}/") for p in tree):
            folder = posixpath.dirname(folder)
        return folder

    def suggest_paths(self, path: str, limit: int = 3) -> list[str]:
        """Files anywhere in the repo with the same name as a path that was not found."""
        missing = normalise(path)
        name = posixpath.basename(missing).lower()
        return [p for p in self._tree()
                if posixpath.basename(p).lower() == name and p != missing][:limit]
