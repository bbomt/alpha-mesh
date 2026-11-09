"""The review trace (PMAT-325): every tool call and its result, exactly as the model received them.

One JSONL file per review, one object per line, shaped like Langfuse observations so Langfuse can
replace the file later (PMAT-262):

    {"type": "trace", "id", "name": "review", "timestamp", "metadata": {repo, base_sha, head_sha, model}}
    {"type": "span",  "id", "trace_id", "name": "file_review", "input": {"file"},
                      "output": {"files_read", "incomplete", "tool_calls_made"}, "start_time", "end_time"}
    {"type": "tool",  "id", "trace_id", "parent_observation_id", "name", "input": <arguments>,
                      "output": <the tool message's content as sent>, "start_time"}

TracingLLM records at the model boundary, inside GuardedLLM: it sees the messages after the
PolicyFileReader (an excluded file is already a refusal) and after the redactor. A file review's
records are held in memory and written when its span ends, so a review stopped part way leaves no
partial span. The trace is never part of the graph state, so it never reaches the checkpoint store.
"""

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Span:
    def __init__(self, trace_id: str, file: str) -> None:
        self.id = uuid.uuid4().hex
        self.trace_id = trace_id
        self.file = file
        self.start_time = _now()
        self.tools: list[dict] = []
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

    def records(self) -> list[dict]:
        span = {"type": "span", "id": self.id, "trace_id": self.trace_id, "name": "file_review",
                "input": {"file": self.file}, "output": self.output,
                "start_time": self.start_time, "end_time": _now()}
        return [span, *self.tools]


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
        return self._inner.chat(messages, tools)


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
