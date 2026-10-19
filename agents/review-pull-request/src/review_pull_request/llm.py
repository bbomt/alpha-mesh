"""Model client: a LangChain chat model behind the stages' chat(messages, tools) interface.

session.py builds the chat model (ChatOpenAI for every OpenAI-compatible endpoint). This module
maps today's OpenAI-format message dicts in and a ChatResponse out, retries what the SDK does not
(malformed replies, a 404 on a call with tools) and turns every failure into LLMError.
HTTP retries (429, 5xx, timeouts) are the SDK's own.
"""

import json
import traceback
from dataclasses import dataclass

import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda


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


ATTEMPTS = 3
RETRY_BACKOFF = {"multiplier": 2, "max": 8}  # seconds between our own retries, with jitter

# What a malformed 200 body raises inside the chat model class (non-JSON, no choices, bad arguments).
_MALFORMED_ERRORS = (ValueError, TypeError, KeyError, IndexError, AttributeError)


class _Malformed(Exception):
    """A reply that cannot be used: can be a one-off provider glitch, so retried."""


class _NoToolEndpoint(Exception):
    """404 on a call with tools: on OpenRouter, no tool-capable provider is reachable right now."""


def _provider_error(e: Exception) -> str | None:
    """The message of an error object in a 200 body: ChatOpenAI raises it as ValueError(body["error"])."""
    if not (isinstance(e, ValueError) and len(e.args) == 1
            and traceback.extract_tb(e.__traceback__)[-1].name == "_create_chat_result"):
        return None
    err = e.args[0]
    return err.get("message", err) if isinstance(err, dict) else err


def _to_response(message: AIMessage) -> ChatResponse:
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

        def call(msgs):
            try:
                reply = model.invoke(msgs)
            except LLMError:
                raise
            except openai.NotFoundError as e:
                if self._not_found:
                    raise LLMError(f"{self._not_found}\n  Provider reply: {e}") from e
                if tools:
                    raise _NoToolEndpoint(str(e)) from e
                raise LLMError(f"API returned 404: {e}") from e
            except openai.APIError as e:
                raise LLMError(str(e)) from e
            except _MALFORMED_ERRORS as e:
                if (message := _provider_error(e)) is not None:  # deterministic, so never retried
                    raise LLMError(f"API returned an error object: {message}") from e
                raise _Malformed(f"{type(e).__name__}: {e}") from e
            return _to_response(reply)

        retrying = RunnableLambda(call).with_retry(
            retry_if_exception_type=(_Malformed, _NoToolEndpoint),
            stop_after_attempt=ATTEMPTS,
            exponential_jitter_params=RETRY_BACKOFF,
        )
        try:
            return retrying.invoke(messages)
        except _Malformed as e:
            raise LLMError(f"Malformed API response ({e})") from e
        except _NoToolEndpoint as e:
            raise LLMError(
                f"model '{self._model}' has no endpoint that supports tool calls\n"
                f"  at {self._api_host} (404, after {ATTEMPTS} attempts): {e}\n"
                "  The review needs tool calls (read_file). Run it again with a model that supports them:\n"
                "    mesh review-pull-request <url> --model <provider/model>"
            ) from e


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
