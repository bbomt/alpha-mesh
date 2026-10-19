"""Model call errors: a real ChatOpenAI behind LangChainLLM, against a scripted HTTP transport.

Ported from test_llm.py (PMAT-265): malformed 200 bodies are retried, then become LLMError.
A 404 on a call with tools is retried with tools, never without them (spec 3.9).
"""

import functools
import json

import httpx
import pytest
from langchain_openai import ChatOpenAI

from review_pull_request.config import Config
from review_pull_request.llm import LLMError
from review_pull_request.session import ReviewSession
from tests.helpers import StubReader

OK = {"id": "1", "object": "chat.completion", "created": 1, "model": "m",
      "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr("review_pull_request.llm.RETRY_BACKOFF", {"multiplier": 0, "max": 0, "jitter": 0})


def _tool_call_body(arguments: str):
    return {**OK, "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": arguments}}]}}]}


def _llm(monkeypatch, responses, model="openai/gpt-oss-120b", url="https://openrouter.ai/api"):
    """The session's review model, its HTTP answered in order by `responses` [(status, body)]."""
    sent = []
    replies = iter(responses)

    def handler(request):
        sent.append(json.loads(request.content))
        status, body = next(replies)
        content = body if isinstance(body, (str, bytes)) else json.dumps(body)
        return httpx.Response(status, content=content,
                              headers={"content-type": "application/json", "retry-after-ms": "1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("review_pull_request.session.ChatOpenAI", functools.partial(ChatOpenAI, http_client=client))
    config = Config(model=model, api_url=url, api_token="t" * 12)
    return ReviewSession.create(config, inner_reader=StubReader()).review_llm, sent


def test_non_json_200_body_raises_llm_error(monkeypatch):
    llm, _ = _llm(monkeypatch, [(200, b"<html>")] * 3)
    with pytest.raises(LLMError):
        llm.chat(MESSAGES, None)


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{}]}])
def test_200_body_without_choices_raises_llm_error(monkeypatch, payload):
    llm, _ = _llm(monkeypatch, [(200, payload)] * 3)
    with pytest.raises(LLMError):
        llm.chat(MESSAGES, None)


def test_non_object_tool_call_arguments_raise_llm_error(monkeypatch):
    llm, _ = _llm(monkeypatch, [(200, _tool_call_body("[1, 2]"))] * 3)
    with pytest.raises(LLMError):
        llm.chat(MESSAGES, TOOLS)


@pytest.mark.parametrize("error", [{"message": "context_length_exceeded: too many tokens"},
                                   "context_length_exceeded: too many tokens"])
def test_provider_error_object_in_a_200_body_fails_fast_with_its_message(monkeypatch, error):
    llm, sent = _llm(monkeypatch, [(200, {"error": error})] * 3)

    with pytest.raises(LLMError, match="context_length_exceeded: too many tokens"):
        llm.chat(MESSAGES, None)
    assert len(sent) == 1


def test_one_malformed_200_body_is_retried_before_giving_up(monkeypatch):
    llm, sent = _llm(monkeypatch, [(200, {}), (200, OK)])

    result = llm.chat(MESSAGES, None)

    assert result.content == "ok"
    assert len(sent) == 2


def test_a_404_with_tools_is_retried_with_tools_and_succeeds(monkeypatch):
    not_found = (404, {"error": {"message": "No endpoints found that support tool use"}})
    llm, sent = _llm(monkeypatch, [not_found, not_found, (200, OK)])

    result = llm.chat(MESSAGES, TOOLS)

    assert result.content == "ok"
    assert len(sent) == 3
    assert all(body.get("tools") for body in sent), "a call without tools must never be made"


def test_a_404_with_tools_after_three_attempts_names_tool_support_and_the_model_hint(monkeypatch):
    not_found = (404, {"error": {"message": "No endpoints found that support tool use"}})
    llm, sent = _llm(monkeypatch, [not_found] * 3)

    with pytest.raises(LLMError) as exc:
        llm.chat(MESSAGES, TOOLS)

    message = str(exc.value)
    assert "model 'openai/gpt-oss-120b' has no endpoint that supports tool calls" in message
    assert "openrouter.ai" in message and "after 3 attempts" in message
    assert "--model <provider/model>" in message
    assert "No endpoints found that support tool use" in message
    assert len(sent) == 3
    assert all(body.get("tools") for body in sent), "a call without tools must never be made"


def test_a_404_without_tools_fails_at_once(monkeypatch):
    llm, sent = _llm(monkeypatch, [(404, {"error": {"message": "model not found"}})])

    with pytest.raises(LLMError):
        llm.chat(MESSAGES, None)
    assert len(sent) == 1


@pytest.mark.parametrize("tools", [TOOLS, None])
def test_a_404_off_openrouter_fails_at_once_naming_the_url_and_model_as_sent(monkeypatch, tools):
    llm, sent = _llm(monkeypatch, [(404, {"error": {"message": "model not found"}})] * 3,
                     model="meta-llama/Llama-3", url="https://user:pw@gpu.local:8000/serve")

    with pytest.raises(LLMError) as exc:
        llm.chat(MESSAGES, tools)

    message = str(exc.value)
    assert "model 'Llama-3' not found (404) at https://gpu.local:8000/serve/v1/chat/completions" in message
    assert "CODE_REVIEW_MODEL 'meta-llama/Llama-3' was sent as 'Llama-3'" in message
    assert "PMAT-276" in message and "model not found" in message
    assert "user:pw" not in message
    assert len(sent) == 1


def test_a_throttled_call_is_retried_by_the_sdk(monkeypatch):
    llm, sent = _llm(monkeypatch, [(429, {"error": "slow down"}), (200, OK)])

    assert llm.chat(MESSAGES, None).content == "ok"
    assert len(sent) == 2


def test_tool_calls_come_back_as_tool_calls_and_messages_go_out_in_openai_format(monkeypatch):
    llm, sent = _llm(monkeypatch, [(200, _tool_call_body('{"path": "a.py", "reason": "r"}'))])
    history = [
        {"role": "user", "content": "review"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c0", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "b.py"}'}}]},
        {"role": "tool", "tool_call_id": "c0", "content": "file b"},
    ]

    result = llm.chat(history, TOOLS)

    assert [(tc.id, tc.name, tc.arguments) for tc in result.tool_calls] == \
        [("c1", "read_file", {"path": "a.py", "reason": "r"})]
    body = sent[0]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["temperature"] == 0.2
    assert body["tool_choice"] == "auto"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "tool"]
    assert body["messages"][1]["tool_calls"][0]["id"] == "c0"
    assert body["messages"][2] == {"role": "tool", "tool_call_id": "c0", "content": "file b"}
