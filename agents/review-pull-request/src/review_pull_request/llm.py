"""httpx-based OpenAI-compatible LLM client.

No vendor SDK — makes raw HTTP calls to /v1/chat/completions.
Works with OpenRouter, Anthropic compat layer, Ollama, OpenAI, etc.
"""

import json
import time
from dataclasses import dataclass

import httpx


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


TRANSIENT_ERROR_CODES = (429, 500, 502, 503)
RETRIES = 3
BACKOFF = [2, 4, 8]


def _parse_chat_response(response) -> ChatResponse:
    """Parse a 200 response body; any malformed body is an LLMError."""
    try:
        choice = response.json()["choices"][0]
        if choice.get("finish_reason", "") == "tool_calls":
            tool_calls = []
            for tc in choice.get("message", {}).get("tool_calls", []):
                func = tc.get("function", {})
                arguments = json.loads(func.get("arguments", "{}"))
                if not isinstance(arguments, dict):
                    raise ValueError(f"tool call arguments must be an object, got {type(arguments).__name__}")
                tool_calls.append(ToolCall(
                    id=tc["id"],
                    name=func["name"],
                    arguments=arguments,
                ))
            return ChatResponse(content=None, tool_calls=tool_calls)
        return ChatResponse(content=choice["message"].get("content"), tool_calls=[])
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
        raise LLMError(f"Malformed API response ({type(e).__name__}: {e})") from e


def chat(
    messages: list[dict],
    tools: list[dict] | None,
    config,
) -> ChatResponse:
    """Send a chat completion request to the OpenAI-compatible endpoint.

    Args:
        messages: list of {"role": ..., "content": ...} dicts
        tools: list of OpenAI tool schemas (optional)
        config: Config instance with model, api_url, api_token

    Returns:
        ChatResponse with either content or tool_calls
    """
    payload: dict = {
        "model": config.model,
        "temperature": 0.2,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    url = f"{config.api_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.api_token}",
        "Content-Type": "application/json",
    }

    last_error = ""
    for attempt in range(RETRIES):
        try:
            response = httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=120.0,
            )
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"

            if response.status_code == 404 and tools:
                # Some providers doesn't support tools. Retry without them.
                retry_payload = {k: v for k, v in payload.items() if k != "tools"}
                retry_payload.pop("tool_choice", None)
                retry_response = httpx.post(
                    url,
                    json=retry_payload,
                    headers=headers,
                    timeout=120.0,
                )
                last_error = f"404 with tools, retry: {retry_response.status_code}"
                if retry_response.status_code == 200:
                    return _parse_chat_response(retry_response)
                raise LLMError(f"API returned 404 with tools, retry also failed: {last_error}")

            if response.status_code in TRANSIENT_ERROR_CODES and attempt < RETRIES - 1:
                sleep_time = BACKOFF[attempt] if attempt < len(BACKOFF) else BACKOFF[-1]
                time.sleep(sleep_time)
                continue

            if response.status_code != 200:
                raise LLMError(f"API returned {response.status_code}: {response.text[:200]}")

            try:
                return _parse_chat_response(response)
            except LLMError as e:  # a malformed 200 body can be a one-off provider glitch
                last_error = str(e)
                if attempt < RETRIES - 1:
                    time.sleep(BACKOFF[attempt] if attempt < len(BACKOFF) else BACKOFF[-1])
                    continue
                raise

        except httpx.TimeoutException:
            last_error = "Request timed out"
            if attempt < RETRIES - 1:
                sleep_time = BACKOFF[attempt] if attempt < len(BACKOFF) else BACKOFF[-1]
                time.sleep(sleep_time)
                continue
            raise LLMError("Request timed out after retries.")

        except httpx.HTTPError as e:
            last_error = str(e)
            if attempt < RETRIES - 1:
                sleep_time = BACKOFF[attempt] if attempt < len(BACKOFF) else BACKOFF[-1]
                time.sleep(sleep_time)
                continue
            raise LLMError(f"HTTP error: {e}")

    raise LLMError(f"Failed after {RETRIES} retries. Last error: {last_error}")
