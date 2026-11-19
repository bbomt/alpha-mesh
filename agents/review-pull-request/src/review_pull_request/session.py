"""ReviewSession — Facade that wires one review run together.

It builds the PathPolicy once and hands the stages only wrapped collaborators:
  * review_llm                : model client wrapped in GuardedLLM (secrets redacted)
  * reader                    : file reader wrapped in PolicyFileReader (excluded paths refused)
  * safe_diff(raw)            : a SafeDiff (excluded files removed)
  * folder_listing(folder)    : a folder's entries, from the policy-filtered repo tree
  * nearest_folder(path)      : the closest folder of a path that exists in that tree
  * suggest_paths(path)       : files anywhere in that tree with the same name as a missing path
  * note_missing(path, file)  : records a missing path the model did not recover from (for the report)
  * trace                     : the review trace (trace.py): one span per file review; its tool calls
                                are recorded inside the guard, as the model was sent them
Stages receive the session, not the raw pieces, so they have no way around the policy.
"""

import posixpath
import sys
import urllib.request
from urllib.parse import urlparse

import httpx
from langchain_openai import ChatOpenAI

from review_pull_request import llm
from review_pull_request.guard import GuardedLLM, Redactor
from review_pull_request.llm import DRY_RUN_MODEL, DryRunChatModel, LangChainLLM
from review_pull_request.policy import PathPolicy, normalise
from review_pull_request.readers import PolicyFileReader
from review_pull_request.safe_diff import SafeDiff
from review_pull_request.trace import Trace, TracingLLM
from review_pull_request.watchdog import WatchdogTransport


OPENROUTER_HOST = "openrouter.ai"


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:  # e.g. unbalanced IPv6 brackets
        return ""


def _safe_url(url: str) -> str:
    """URL without userinfo or query string, safe to print."""
    try:
        p = urlparse(url)
    except ValueError:  # e.g. unbalanced IPv6 brackets
        return "(invalid URL)"
    if not p.hostname:
        return "(invalid URL)"
    try:
        port = f":{p.port}" if p.port else ""
    except ValueError:  # malformed port in a hand-edited .env
        return "(invalid URL)"
    return f"{p.scheme}://{p.hostname}{port}{p.path}"


def _sent(config, model: str) -> tuple[str, str]:
    """The model name and base URL as sent: openrouter.ai gets the full name, other hosts get it
    without the `provider/` prefix; /v1 is appended to the URL if missing (spec 3.7, PMAT-276)."""
    if _host(config.api_url) != OPENROUTER_HOST and "/" in model:
        model = model.split("/", 1)[1]
    base_url = config.api_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    return model, base_url


def not_found_reason(config) -> str | None:
    """Off openrouter.ai, a 404 most likely comes from the name and URL rules: say what was sent."""
    if _host(config.api_url) == OPENROUTER_HOST or config.model == DRY_RUN_MODEL:
        return None
    model, base_url = _sent(config, config.model)
    return (f"model '{model}' not found (404) at {_safe_url(base_url)}/chat/completions\n"
            f"  model '{config.model}' was sent as '{model}': for hosts other than openrouter.ai\n"
            "  the provider/ prefix is stripped, and /v1 is appended to CODE_REVIEW_URL if missing.\n"
            "  Model ids with a real '/' and URLs not ending in /v1 are not supported yet (PMAT-276).")


def make_chat_model(config, model: str):
    """The chat model for CODE_REVIEW_URL: ChatOpenAI for every OpenAI-compatible endpoint.

    It sends config.temperature, [model] temperature in review_config.toml (PMAT-338), on every call.
    Names are `{provider}/{modelname}`: openrouter.ai gets the full name, other hosts get it
    without the `provider/` prefix. Key and URL come from config, never a provider env var.
    The reply is streamed so a hung call can be told from a slow one (PMAT-236): the watchdog
    transport and LangChainLLM's retry loop own timeouts and retries, so the SDK never retries.
    """
    if model == DRY_RUN_MODEL:
        return DryRunChatModel()
    model, base_url = _sent(config, model)
    client = httpx.Client(transport=WatchdogTransport(http_transport(base_url)),
                          timeout=httpx.Timeout(llm.IDLE_TIMEOUT, connect=30))
    return ChatOpenAI(model=model, api_key=config.api_token, base_url=base_url, temperature=config.temperature,
                      streaming=True, max_retries=0, http_client=client)


def http_transport(url: str) -> httpx.BaseTransport:
    """The network transport under the watchdog; tests replace it with a scripted one.

    A client given its own transport ignores the proxy env vars, so the proxy for `url` is
    taken from the environment here, honouring NO_PROXY like a default httpx client (PMAT-290).
    """
    parsed = urlparse(url)
    proxies = urllib.request.getproxies_environment()
    proxy = None if urllib.request.proxy_bypass_environment(parsed.hostname, proxies) \
        else proxies.get(parsed.scheme) or proxies.get("all")
    return httpx.HTTPTransport(proxy=proxy)


class ReviewSession:
    def __init__(self, config, policy, review_llm, reader) -> None:
        self.config = config
        self.policy = policy
        self.review_llm = review_llm
        self.reader = reader
        self._files: list[str] | None = None  # repo tree minus excluded files, loaded once
        self._listed = False
        self.missing_paths: dict[str, list[str]] = {}  # missing path -> files whose review asked for it
        self.trace = Trace()  # create() replaces it with the one recording at the model boundary

    @classmethod
    def create(cls, config, inner_reader, chat_model_factory=None, secrets=(), trace=None) -> "ReviewSession":
        factory = chat_model_factory or make_chat_model
        policy = PathPolicy(config.exclusions)
        redactor = Redactor([config.api_token, *secrets])
        llm = LangChainLLM(factory(config, config.model), config.model, _host(config.api_url) or "(invalid URL)",
                           not_found=not_found_reason(config))
        trace = trace or Trace()
        session = cls(
            config=config,
            policy=policy,
            # The trace records inside the guard: exactly what the model is sent, already redacted.
            review_llm=GuardedLLM(TracingLLM(llm, trace), redactor),
            reader=PolicyFileReader(inner_reader, policy),
        )
        session.trace = trace
        return session

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
