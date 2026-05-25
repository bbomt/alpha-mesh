"""httpx-based OpenAI-compatible LLM client.

No vendor SDK — makes raw HTTP calls to /v1/chat/completions.
Works with OpenRouter, Anthropic compat layer, Ollama, OpenAI, etc.
"""

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

            if response.status_code in TRANSIENT_ERROR_CODES and attempt < RETRIES - 1:
                sleep_time = BACKOFF[attempt] if attempt < len(BACKOFF) else BACKOFF[-1]
                time.sleep(sleep_time)
                continue

            if response.status_code != 200:
                raise LLMError(f"API returned {response.status_code}: {response.text[:200]}")

            data = response.json()
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "")

            if finish_reason == "tool_calls":
                tool_calls = []
                for tc in choice.get("message", {}).get("tool_calls", []):
                    func = tc.get("function", {})
                    arguments_str = func.get("arguments", "{}")
                    import json
                    args = json.loads(arguments_str)
                    tool_calls.append(ToolCall(
                        id=tc["id"],
                        name=func["name"],
                        arguments=args,
                    ))
                return ChatResponse(content=None, tool_calls=tool_calls)
            else:
                return ChatResponse(content=choice["message"].get("content"), tool_calls=[])

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
