"""Model call errors: a real ChatOpenAI behind LangChainLLM, against a scripted HTTP transport.

Ported from test_llm.py (PMAT-265): malformed 200 bodies are retried, then become LLMError.
A 404 on a call with tools is retried with tools, never without them (spec 3.9).
PMAT-236: transient errors are retried with backoff, credits and permanent errors fail at once,
and a call that stops sending reply data (or runs past its deadline) is cut off.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from review_pull_request import llm as llm_module
from review_pull_request.config import Config
from review_pull_request.llm import LLMError, _backoff
from review_pull_request.session import ReviewSession
from tests.helpers import StubReader

OK = {"id": "1", "object": "chat.completion", "created": 1, "model": "m",
      "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Backoff sleeps, recorded instead of slept."""
    slept = []
    monkeypatch.setattr("review_pull_request.llm._sleep", slept.append)
    return slept


def _sse(body) -> bytes:
    """A reply as the SSE stream a streaming call receives. A completion becomes one chunk;
    anything else (an error object, a malformed payload) is sent as a single data event."""
    choices = body.get("choices") if isinstance(body, dict) else None
    if not (choices and "message" in choices[0]):
        return f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n".encode()
    message = choices[0]["message"]
    delta = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **tc} for i, tc in enumerate(message["tool_calls"])]
    chunk = {"id": body["id"], "object": "chat.completion.chunk", "created": 1, "model": body["model"],
             "choices": [{"index": 0, "delta": delta, "finish_reason": choices[0]["finish_reason"]}]}
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()


def _tool_call_body(arguments: str):
    return {**OK, "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": arguments}}]}}]}


def _llm(monkeypatch, responses, model="openai/gpt-oss-120b", url="https://openrouter.ai/api"):
    """The session's review model, its HTTP answered in order by `responses`
    [(status, body)] or [(status, body, headers)]. A 200 is streamed as SSE."""
    sent = []
    replies = iter(responses)

    def handler(request):
        sent.append(json.loads(request.content))
        status, body, *headers = next(replies)
        if isinstance(body, bytes):
            content, kind = body, "text/event-stream"
        elif status == 200:
            content, kind = _sse(body), "text/event-stream"
        else:
            content, kind = json.dumps(body), "application/json"
        return httpx.Response(status, content=content, headers={"content-type": kind, **(headers[0] if headers else {})})

    monkeypatch.setattr("review_pull_request.session.http_transport", lambda: httpx.MockTransport(handler))
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


def test_a_throttled_call_is_retried(monkeypatch):
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


# --- PMAT-236: classification and backoff ----------------------------------------------------

@pytest.mark.parametrize("error", [{"message": "A Timeout Occurred"},
                                   {"message": "Upstream idle timeout exceeded"},
                                   {"message": "busy", "code": 408},
                                   {"message": "Provider is overloaded"}])
def test_a_transient_error_object_is_retried_and_the_next_attempt_succeeds(monkeypatch, error):
    llm, sent = _llm(monkeypatch, [(200, {"error": error}), (200, OK)])

    assert llm.chat(MESSAGES, None).content == "ok"
    assert len(sent) == 2


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_transient_http_statuses_are_retried(monkeypatch, status):
    llm, sent = _llm(monkeypatch, [(status, {"error": {"message": "try later"}}), (200, OK)])

    assert llm.chat(MESSAGES, None).content == "ok"
    assert len(sent) == 2


@pytest.mark.parametrize("header, expected", [({"retry-after": "7"}, 7), ({"retry-after-ms": "1500"}, 1.5),
                                              ({"retry-after": "500"}, llm_module.BACKOFF_CAP)])
def test_retry_after_on_a_429_sets_the_delay_capped(monkeypatch, sleeps, header, expected):
    llm, _ = _llm(monkeypatch, [(429, {"error": {"message": "rate limited"}}, header), (200, OK)])

    llm.chat(MESSAGES, None)

    assert sleeps == [expected]


@pytest.mark.parametrize("status, body", [
    (429, {"error": {"message": "OpenRouter could not verify available credits for this request in time. "
                                "Retry shortly.", "code": 429,
                     "metadata": {"limit_source": "openrouter_admission_control"}}}),
    (402, {"error": {"message": "Insufficient credits. Add more using https://openrouter.ai/credits"}}),
])
def test_credits_or_quota_errors_fail_at_once_naming_credits(monkeypatch, status, body):
    llm, sent = _llm(monkeypatch, [(status, body)] * 6)

    with pytest.raises(LLMError, match="credits"):
        llm.chat(MESSAGES, None)
    assert len(sent) == 1


def test_an_invalid_key_fails_at_once(monkeypatch):
    llm, sent = _llm(monkeypatch, [(401, {"error": {"message": "invalid api key"}})] * 6)

    with pytest.raises(LLMError, match="invalid api key"):
        llm.chat(MESSAGES, None)
    assert len(sent) == 1


def test_a_transient_error_on_every_attempt_gives_up_with_the_provider_text(monkeypatch, sleeps, capsys):
    llm, sent = _llm(monkeypatch, [(503, {"error": {"message": "no capacity right now"}})] * 10)

    with pytest.raises(LLMError, match="no capacity right now") as exc:
        llm.chat(MESSAGES, None)

    attempts = llm_module.ATTEMPTS["transient"]
    assert len(sent) == attempts and len(sleeps) == attempts - 1
    assert f"after {attempts} attempts" in str(exc.value)
    err = capsys.readouterr().err
    assert f"(attempt 2/{attempts})" in err and "retrying in" in err


def test_retrying_stops_when_the_retry_budget_is_spent(monkeypatch, sleeps):
    monkeypatch.setattr("review_pull_request.llm.RETRY_BUDGET", 0.5)
    llm, sent = _llm(monkeypatch, [(503, {"error": {"message": "busy"}})] * 10)

    with pytest.raises(LLMError, match="retry budget"):
        llm.chat(MESSAGES, None)
    assert len(sent) == 1 and sleeps == []


def test_backoff_is_exponential_capped_and_jitter_stays_in_bounds():
    for attempt in range(1, 10):
        base = min(llm_module.BACKOFF_CAP, llm_module.BACKOFF_BASE * 2 ** (attempt - 1))
        delays = [_backoff(attempt) for _ in range(200)]
        assert all(base / 2 <= d <= base for d in delays)
        assert len(set(delays)) > 1, "no jitter"
    assert _backoff(1) <= 2 and _backoff(30) <= llm_module.BACKOFF_CAP
    assert _backoff(1, retry_after=5000) == llm_module.BACKOFF_CAP


# --- PMAT-236: idle limit and deadline, over a real local HTTP server --------------------------

class _Trickle(BaseHTTPRequestHandler):
    """Answers 200 and then trickles `line` every 50 ms until the test ends."""
    line = b": OPENROUTER PROCESSING\n\n"
    stop = threading.Event()

    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"]))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        try:
            while not self.stop.is_set():
                self.wfile.write(self.line)
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:  # the client cut the call off
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def trickle(monkeypatch):
    """A local server whose replies never finish; yields (handler class, base URL, requests seen)."""
    seen = []

    class Handler(_Trickle):
        stop = threading.Event()

        def do_POST(self):
            seen.append(1)
            super().do_POST()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Handler, f"http://127.0.0.1:{server.server_port}", seen
    Handler.stop.set()
    server.shutdown()


def _local_llm(url):
    return ReviewSession.create(Config(model="m", api_url=url, api_token="t" * 12),
                                inner_reader=StubReader()).review_llm


def test_keep_alive_padding_without_reply_data_is_cut_off_at_the_idle_limit(monkeypatch, trickle):
    handler, url, seen = trickle
    monkeypatch.setattr("review_pull_request.llm.IDLE_TIMEOUT", 0.5)

    started = time.monotonic()
    with pytest.raises(LLMError, match="no reply data for"):
        _local_llm(url).chat(MESSAGES, None)

    assert len(seen) == llm_module.ATTEMPTS["timeout"]
    assert time.monotonic() - started < 5


def test_a_reply_still_streaming_at_the_deadline_is_cut_off(monkeypatch, trickle):
    handler, url, seen = trickle
    chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": None}]}
    handler.line = f"data: {json.dumps(chunk)}\n\n".encode()  # real tokens, forever
    monkeypatch.setattr("review_pull_request.llm.IDLE_TIMEOUT", 0.5)
    monkeypatch.setattr("review_pull_request.llm.DEADLINE_MIN", 1)
    monkeypatch.setattr("review_pull_request.llm.DEADLINE_MAX", 1)

    started = time.monotonic()
    with pytest.raises(LLMError, match="deadline"):
        _local_llm(url).chat(MESSAGES, None)

    assert len(seen) == llm_module.ATTEMPTS["timeout"]
    assert time.monotonic() - started < 6


def test_the_deadline_scales_with_prompt_size_between_its_bounds():
    small = llm_module._deadline([{"role": "user", "content": "x"}])
    large = llm_module._deadline([{"role": "user", "content": "x" * 50_000}])
    huge = llm_module._deadline([{"role": "user", "content": "x" * 10_000_000}])

    assert small == llm_module.DEADLINE_MIN
    assert llm_module.DEADLINE_MIN < large < llm_module.DEADLINE_MAX
    assert huge == llm_module.DEADLINE_MAX
