"""Per-call time limits for model calls, checked on the reply bytes as they arrive (PMAT-236).

httpx's timeout restarts on every byte, so a provider that pads a stalled reply with keep-alive
whitespace or SSE comments is never cut off. WatchdogTransport checks every chunk of a reply
made inside `watching(idle, total)`:
  * idle:     no reply data for `idle` seconds (padding and `: comment` lines are not data)
  * deadline: `total` seconds of wall time for the whole call
Complete silence is httpx's own read timeout, set to the same idle limit in session.py.
"""

import contextvars
import time
from contextlib import contextmanager

import httpx

_MAX_LINE_START = 256  # a longer unfinished line is reply data, not a keep-alive comment


class CallTimeout(httpx.ReadTimeout):
    """A model call ran past its idle limit or its deadline."""


class _Watch:
    def __init__(self, idle: float, total: float) -> None:
        now = time.monotonic()
        self.idle, self.total = idle, total
        self.deadline = now + total
        self.last_data = now
        self._tail = b""  # the unfinished last line seen so far

    def _has_data(self, chunk: bytes) -> bool:
        *lines, self._tail = (self._tail + chunk).split(b"\n")
        data = any(line.strip() and not line.lstrip().startswith(b":") for line in lines)
        if len(self._tail) > _MAX_LINE_START:
            data, self._tail = True, self._tail[:1]  # keep the first byte: it decides the line's kind
        return data

    def check(self, chunk: bytes) -> None:
        now = time.monotonic()
        if self._has_data(chunk):
            self.last_data = now
        if now > self.deadline:
            raise CallTimeout(f"call exceeded its {self.total:.0f}s deadline")
        if now - self.last_data > self.idle:
            raise CallTimeout(f"no reply data for {self.idle:.0f}s (call hung)")


_current: contextvars.ContextVar[_Watch | None] = contextvars.ContextVar("model_call_watch", default=None)


@contextmanager
def watching(idle: float, total: float):
    """Replies received inside this block are cut off at the idle limit or the deadline."""
    token = _current.set(_Watch(idle, total))
    try:
        yield
    finally:
        _current.reset(token)


class _WatchedStream(httpx.SyncByteStream):
    def __init__(self, inner, watch: _Watch) -> None:
        self._inner = inner
        self._watch = watch

    def __iter__(self):
        for chunk in self._inner:
            self._watch.check(chunk)
            yield chunk

    def close(self) -> None:
        self._inner.close()


class WatchdogTransport(httpx.BaseTransport):
    """Wraps a transport; replies made inside `watching()` are checked chunk by chunk."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        if (watch := _current.get()) is not None:
            response.stream = _WatchedStream(response.stream, watch)
        return response

    def close(self) -> None:
        self._inner.close()
