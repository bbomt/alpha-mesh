"""Shared test doubles for the review pipeline."""

from types import SimpleNamespace

from review_pull_request.session import ReviewSession


class FakeLLM:
    """A model client: `chat(messages, tools)`; answers come from a callable or a fixed response."""

    def __init__(self, responder=None, content=None):
        self.responder = responder
        self.content = content
        self.calls = []  # every (messages, tools) the model was shown

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        if self.responder is not None:
            return self.responder(messages, tools)
        return SimpleNamespace(content=self.content, tool_calls=[])


class StubReader:
    """A file reader double: `read_file(path, max_lines)`."""

    def __init__(self, files=None, default=None):
        self.files = files or {}
        self.default = default
        self.asked = []

    def read_file(self, path, max_lines):
        from review_pull_request.readers import NotFound

        self.asked.append(path)
        if path in self.files:
            return self.files[path]
        if self.default is not None:
            return self.default
        return NotFound(f"Error: file not found: {path}")

    def close(self):
        pass


def make_session(config, llm=None, reader=None):
    """Build a ReviewSession whose model and file reader are fakes (policy/guard stay real)."""
    review_llm = llm or FakeLLM(content='{"summary": "ok", "issues": []}')

    def factory(cfg, model):
        return review_llm

    return ReviewSession.create(config, inner_reader=reader or StubReader(), llm_factory=factory)


def http_llm_factory(responder):
    """Stand-in for HttpLLM: patch("review_pull_request.session.HttpLLM", http_llm_factory(fn)).

    `fn(messages, tools)` answers every model call; the overview call has tools=None.
    """
    def factory(config, model=None):
        return FakeLLM(responder)
    return factory
