"""Model client: a LangChain chat model behind the stages' chat(messages, tools) interface.

session.py builds the chat model (ChatOpenAI for every OpenAI-compatible endpoint, streaming,
SDK retries off). This module maps today's OpenAI-format message dicts in and a ChatResponse out,
and owns the retry policy (PMAT-236): every failed attempt is classified in one place, `_classify`.
  * timeout   : no reply data for IDLE_TIMEOUT, or past the call's deadline: retried once
  * transient : 408/429/5xx, connection errors, provider error objects saying timeout,
                rate limit, overloaded or temporarily unavailable: retried with backoff
  * credits   : 402, or a credits/quota error (also a 429): fails at once, waiting cannot fix it
  * permanent : anything else the provider rejects (400/401/403, unknown model): fails at once
Malformed replies and a 404 on a call with tools keep their own retries.
Every failure ends as LLMError (fail noisily: abort, non-zero exit, nothing posted).
"""

import json
import random
import re
import sys
import time
from dataclasses import dataclass

import httpx
import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from review_pull_request.watchdog import CallTimeout, watching


class LLMError(Exception):
    """Raised on non-retryable LLM API errors."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResponse:
    content: str | None
    tool_calls: list[ToolCall]


IDLE_TIMEOUT = 60                     # seconds without reply data: the call is hung
DEADLINE_MIN, DEADLINE_MAX = 120, 300  # seconds per call, scaled with the prompt between these
PROMPT_CHARS_PER_SECOND = 500
ATTEMPTS = {"timeout": 2, "transient": 6, "malformed": 3, "no_tool_endpoint": 3}  # attempts in total
BACKOFF_BASE, BACKOFF_CAP = 2, 60     # seconds: 2, 4, 8 ... capped, with equal jitter
RETRY_BUDGET = 15 * 60                # seconds spent on one call, retries included
_sleep = time.sleep

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
_TRANSIENT_TEXT = re.compile(r"time[d ]?out|rate.?limit|overloaded|temporarily unavailable", re.I)
_CREDITS_TEXT = re.compile(r"credit|quota|admission_control", re.I)

# What a malformed reply raises inside the chat model class (non-JSON, no choices, bad arguments).
_MALFORMED_ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError)


class _Malformed(Exception):
    """A reply that cannot be used: can be a one-off provider glitch, so retried."""


def _error_text(e: openai.APIError) -> str:
    """The provider's message plus its error body, so codes and metadata can be matched too."""
    body = e.body if isinstance(e.body, (dict, list, str)) else ""
    return f"{e.message} {json.dumps(body) if not isinstance(body, str) else body}".strip()


def _watchdog_cause(e: BaseException | None) -> CallTimeout | None:
    """The watchdog's own timeout, which the SDK and LangChain wrap in their generic ones."""
    while e is not None and not isinstance(e, CallTimeout):
        e = e.__cause__ or e.__context__
    return e


def _classify(e: Exception) -> tuple[str, str] | None:
    """(kind, reason) for one failed attempt; None for an exception that is not a model failure."""
    if isinstance(e, (openai.APITimeoutError, httpx.TimeoutException)):
        return "timeout", str(_watchdog_cause(e) or e) or "request timed out"
    if isinstance(e, openai.APIStatusError):
        status, text = e.status_code, _error_text(e)
        if status == 402 or (status == 429 and _CREDITS_TEXT.search(text)):
            return "credits", e.message
        if status == 404:
            return "not_found", str(e)
        return ("transient" if status in TRANSIENT_STATUS else "permanent"), e.message
    if isinstance(e, (openai.APIConnectionError, httpx.TransportError)):
        return "transient", f"connection error: {e}"
    if isinstance(e, openai.APIError):  # an error object inside a 200 (streamed) reply
        text = _error_text(e)
        message = e.body if isinstance(e.body, str) else e.message  # a bare string error has no "message"
        if e.code == "402" or _CREDITS_TEXT.search(text):
            return "credits", message
        if e.code in {str(s) for s in TRANSIENT_STATUS} or _TRANSIENT_TEXT.search(message):
            return "transient", f"provider error: {message}"
        return "permanent", f"API returned an error object: {message}"
    if isinstance(e, _Malformed):
        return "malformed", str(e)
    if isinstance(e, _MALFORMED_ERRORS):
        return "malformed", f"{type(e).__name__}: {e}"
    return None


def _retry_after(e: Exception) -> float | None:
    """Seconds the provider asked us to wait (Retry-After on a 429/503), if any."""
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    try:
        if "retry-after-ms" in headers:
            return float(headers["retry-after-ms"]) / 1000
        if "retry-after" in headers:
            return float(headers["retry-after"])
    except ValueError:  # an HTTP date: fall back to our own backoff
        pass
    return None


def _backoff(attempt: int, retry_after: float | None = None) -> float:
    """Seconds to wait after failed attempt `attempt` (1-based): the provider's Retry-After if
    given, else exponential with equal jitter (half fixed, half random). Always capped."""
    if retry_after is not None:
        return min(BACKOFF_CAP, max(0.0, retry_after))
    base = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (attempt - 1))
    return base / 2 + random.uniform(0, base / 2)


def _deadline(messages: list[dict]) -> float:
    """Wall-time limit for one call: a large prompt on a slow provider gets longer, up to DEADLINE_MAX."""
    chars = len(json.dumps(messages, default=str))
    return min(DEADLINE_MAX, DEADLINE_MIN + chars // PROMPT_CHARS_PER_SECOND)


def _to_response(message: AIMessage) -> ChatResponse:
    if not message.text and not message.tool_calls and not message.response_metadata.get("finish_reason"):
        raise _Malformed("empty reply without a finish_reason (no choices, or the stream was cut short)")
    if message.invalid_tool_calls:
        raise _Malformed(f"unparseable tool call: {message.invalid_tool_calls[0].get('error')}")
    if message.tool_calls:
        return ChatResponse(content=None, tool_calls=[
            ToolCall(id=tc["id"], name=tc["name"], arguments=tc["args"]) for tc in message.tool_calls])
    return ChatResponse(content=message.text, tool_calls=[])


class LangChainLLM:
    """The real model client: chat(messages, tools) through a LangChain chat model."""

    def __init__(self, chat_model, model: str, api_host: str, not_found: str | None = None) -> None:
        self._chat_model = chat_model
        self._model = model
        self._api_host = api_host
        self._not_found = not_found  # set off OpenRouter: a 404 then fails at once with this text

    def chat(self, messages: list[dict], tools=None) -> ChatResponse:
        model = self._chat_model.bind_tools(tools, tool_choice="auto") if tools else self._chat_model
        deadline = _deadline(messages)
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            try:
                with watching(IDLE_TIMEOUT, deadline):
                    return _to_response(model.invoke(messages))
            except LLMError:
                raise
            except Exception as e:
                if (classified := _classify(e)) is None:
                    raise
                kind, reason = classified
                if kind == "not_found":
                    kind = self._not_found_kind(reason, tools, e)
                limit = ATTEMPTS.get(kind)
                if limit is None:
                    raise LLMError(self._fails_at_once(kind, reason)) from e
                if attempt >= limit:
                    raise LLMError(self._gave_up(kind, reason, attempt)) from e
                delay = _backoff(attempt, _retry_after(e))
                if time.monotonic() - started + delay > RETRY_BUDGET:
                    raise LLMError(f"model call gave up: retry budget of {RETRY_BUDGET:.0f}s spent "
                                   f"after {attempt} attempt(s): {reason}") from e
                print(f"  retrying in {delay:.0f}s (attempt {attempt + 1}/{limit}): {kind}: {reason}",
                      file=sys.stderr, flush=True)
                _sleep(delay)

    def _not_found_kind(self, reason: str, tools, e: Exception) -> str:
        """A 404: off OpenRouter it fails at once naming what was sent; with tools on OpenRouter
        no tool-capable provider may be reachable right now, so it is retried with tools."""
        if self._not_found:
            raise LLMError(f"{self._not_found}\n  Provider reply: {reason}") from e
        if tools:
            return "no_tool_endpoint"
        raise LLMError(f"API returned 404: {reason}") from e

    def _fails_at_once(self, kind: str, reason: str) -> str:
        if kind == "credits":
            return (f"model provider refused the call for credits or quota: {reason}\n"
                    "  Add credits (or wait for the quota to reset), then rerun: the review resumes "
                    "from its checkpoint.")
        return reason

    def _gave_up(self, kind: str, reason: str, attempts: int) -> str:
        if kind == "malformed":
            return f"Malformed API response ({reason})"
        if kind == "no_tool_endpoint":
            return (f"model '{self._model}' has no endpoint that supports tool calls\n"
                    f"  at {self._api_host} (404, after {attempts} attempts): {reason}\n"
                    "  The review needs tool calls (read_file). Run it again with a model that supports them:\n"
                    "    mesh review-pull-request <url> --model <provider/model>")
        return f"model call failed after {attempts} attempts ({kind}): {reason}"


DRY_RUN_MODEL = "dry-run-model"

DRY_RUN_OVERVIEW = (
    "This pull request refactors the dream core module into a separate agent "
    "directory, following the established pattern from PMAT-189. It introduces "
    "a thin proxy at bin/dream that delegates to agents/dream/dream/bin/dream, "
    "while filtering out system-generated Claude Code messages from the session "
    "data fed to the LLM."
)

DRY_RUN_FILE_SUMMARY = (
    "Delegates to the inner dream bash runner after loading .env "
    "credentials. Adds dry-run handling to skip env validation."
)


class DryRunChatModel(BaseChatModel):
    """--dry-run: a scripted model, no network. The overview call (no tools) gets
    DRY_RUN_OVERVIEW; every file review (with tools) gets a clean result."""

    @property
    def _llm_type(self) -> str:
        return DRY_RUN_MODEL

    def bind_tools(self, tools, **kwargs):
        return self.bind(file_review=True)

    def _generate(self, messages, stop=None, run_manager=None, file_review=False, **kwargs):
        content = (json.dumps({"summary": DRY_RUN_FILE_SUMMARY, "issues": []}) if file_review
                   else DRY_RUN_OVERVIEW)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])
