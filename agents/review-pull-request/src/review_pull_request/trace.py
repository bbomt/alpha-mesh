"""The review trace (PMAT-325): every tool call and its result, exactly as the model received them.

One JSONL file per review, one object per line, shaped like Langfuse observations so Langfuse can
replace the file later (PMAT-262):

    {"type": "trace", "id", "name": "review", "timestamp", "metadata": {repo, base_sha, head_sha, model}}
    {"type": "span",  "id", "trace_id", "name": "file_review", "input": {"file"},
                      "output": {"files_read", "incomplete", "tool_calls_made"}, "start_time", "end_time"}
    {"type": "tool",  "id", "trace_id", "parent_observation_id", "name", "input": <arguments>,
                      "output": <the tool message's content as sent>, "start_time"}
    {"type": "generation", "id", "trace_id", "parent_observation_id", "name": "chat", "model",
                      "model_parameters": {temperature, stream, ...}, "output": {status, model, provider},
                      "start_time"}

A generation is one HTTP request to the model (a retry is another one), recorded by RecordingTransport
(PMAT-338): its parameters as sent on the wire, so a trace shows the temperature the run really sent,
and the model and provider the reply named. Only the body's top-level scalars: never the messages,
the tools or the headers (the token). The overview pass has no span, so its calls are not recorded.

TracingLLM records at the model boundary, inside GuardedLLM: it sees the messages after the
PolicyFileReader (an excluded file is already a refusal) and after the redactor. A file review's
records are held in memory and written when its span ends, so a review stopped part way leaves no
partial span. The trace is never part of the graph state, so it never reaches the checkpoint store.
"""

import contextvars
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Span:
    def __init__(self, trace_id: str, file: str) -> None:
        self.id = uuid.uuid4().hex
        self.trace_id = trace_id
        self.file = file
        self.start_time = _now()
        self.tools: list[dict] = []
        self.generations: list[dict] = []
        self.output: dict = {}
        self.seen = 0  # messages of this review already recorded

    def record_sent(self, messages: list[dict]) -> None:
        """Record the tool messages not seen before, each with the assistant tool call it answers."""
        now = _now()
        for i in range(self.seen, len(messages)):
            message = messages[i]
            if message.get("role") != "tool":
                continue
            call = next((tc for m in reversed(messages[:i]) if m.get("role") == "assistant"
                         for tc in m.get("tool_calls") or [] if tc.get("id") == message.get("tool_call_id")), None)
            function = (call or {}).get("function", {})
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = function.get("arguments")
            self.tools.append({"type": "tool", "id": uuid.uuid4().hex, "trace_id": self.trace_id,
                               "parent_observation_id": self.id, "name": function.get("name"),
                               "input": arguments, "output": message.get("content"), "start_time": now})
        self.seen = len(messages)

    def record_calls(self, calls: list[dict]) -> None:
        for call in calls:
            parameters = dict(call["parameters"])
            self.generations.append({"type": "generation", "id": uuid.uuid4().hex, "trace_id": self.trace_id,
                                     "parent_observation_id": self.id, "name": "chat",
                                     "model": parameters.pop("model", None), "model_parameters": parameters,
                                     "output": call["output"], "start_time": call["start_time"]})

    def records(self) -> list[dict]:
        span = {"type": "span", "id": self.id, "trace_id": self.trace_id, "name": "file_review",
                "input": {"file": self.file}, "output": self.output,
                "start_time": self.start_time, "end_time": _now()}
        return [span, *self.tools, *self.generations]


class Trace:
    """One review's trace. Records nothing until open(): a dry run never opens it."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.id = ""
        self._span: Span | None = None

    def open(self, path, metadata: dict, fresh: bool) -> None:
        """Write to `path`: fresh=True (a review from the start) replaces an old trace; a resume appends."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fresh and self.path.exists():
            self.path.unlink()
        if self.path.exists() and not self.path.read_bytes().endswith(b"\n"):
            with self.path.open("a") as f:  # a run killed mid-write left a torn line: end it
                f.write("\n")
        head = _read(self.path)[:1]
        if head and head[0].get("type") == "trace":
            self.id = head[0]["id"]
        else:
            self.id = uuid.uuid4().hex
            self._write([{"type": "trace", "id": self.id, "name": "review", "timestamp": _now(),
                          "metadata": metadata}])

    @contextmanager
    def file_review(self, file: str):
        """One span; written only if the review ends without an exception."""
        self._span = Span(self.id, file)
        try:
            yield self._span
            if self.path is not None:
                self._write(self._span.records())
        finally:
            self._span = None

    def record_sent(self, messages: list[dict]) -> None:
        if self._span is not None and self.path is not None:  # the overview pass has no span
            self._span.record_sent(messages)

    def record_calls(self, calls: list[dict]) -> None:
        if self._span is not None and self.path is not None:
            self._span.record_calls(calls)

    def _write(self, records: list[dict]) -> None:
        with self.path.open("a") as f:
            f.write("".join(json.dumps(r) + "\n" for r in records))
            f.flush()
            os.fsync(f.fileno())


class TracingLLM:
    """Decorator: same chat() interface; records the messages the wrapped model is sent."""

    def __init__(self, inner, trace) -> None:
        self._inner = inner
        self._trace = trace

    def chat(self, messages: list[dict], tools=None):
        self._trace.record_sent(messages)
        calls: list[dict] = []
        token = _calls.set(calls)
        try:
            return self._inner.chat(messages, tools)
        finally:
            _calls.reset(token)
            self._trace.record_calls(calls)


# The HTTP requests made inside one TracingLLM.chat() (its retries included), or None outside one.
_calls: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar("model_calls", default=None)


def _parameters(request: httpx.Request) -> dict:
    """The request body's top-level scalars: model, temperature, stream... never messages or tools."""
    try:
        body = json.loads(request.content)
    except (httpx.RequestNotRead, ValueError):
        return {}
    if not isinstance(body, dict):
        return {}
    return {k: v for k, v in body.items()
            if k not in ("messages", "tools") and (v is None or isinstance(v, (str, int, float, bool)))}


class _NamedStream(httpx.SyncByteStream):
    """Passes the reply through; notes the first "model" and "provider" its SSE data events name."""

    def __init__(self, inner, output: dict) -> None:
        self._inner = inner
        self._output = output
        self._tail = b""

    def __iter__(self):
        for chunk in self._inner:
            self._read(chunk)
            yield chunk

    def _read(self, chunk: bytes) -> None:
        if "model" in self._output and "provider" in self._output:
            return
        *lines, self._tail = (self._tail + chunk).split(b"\n")
        for line in lines:
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:  # "[DONE]", or not JSON
                continue
            if isinstance(event, dict):
                for key in ("model", "provider"):
                    if isinstance(event.get(key), str):
                        self._output.setdefault(key, event[key])

    def close(self) -> None:
        self._inner.close()


class RecordingTransport(httpx.BaseTransport):
    """Wraps the model's transport; a request made inside TracingLLM.chat() is noted for its span."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        calls = _calls.get()
        if calls is None:
            return self._inner.handle_request(request)
        call = {"parameters": _parameters(request), "output": {}, "start_time": _now()}
        calls.append(call)
        response = self._inner.handle_request(request)
        call["output"]["status"] = response.status_code
        response.stream = _NamedStream(response.stream, call["output"])
        return response

    def close(self) -> None:
        self._inner.close()


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:  # blank, or torn by a run killed mid-write (that span was not written)
            pass
    return records


def load(path) -> dict[str, dict]:
    """file -> {"span": ..., "tools": [...]} for each file review in the trace, in review order.

    If a file was reviewed twice (its span was written, then the run died before its checkpoint was
    saved, so the resume reviewed it again), the last review is the one the result came from.
    """
    records = _read(Path(path))
    tools: dict[str, dict[str, dict]] = {}  # span id -> tool id -> record (a record written twice counts once)
    for r in records:
        if r.get("type") == "tool":
            tools.setdefault(r["parent_observation_id"], {}).setdefault(r["id"], r)
    spans: dict[str, dict] = {}
    for r in records:
        if r.get("type") == "span":
            spans.pop(r["input"]["file"], None)  # the last review moves to the end
            spans[r["input"]["file"]] = {"span": r, "tools": list(tools.get(r["id"], {}).values())}
    return spans
