"""Shared test doubles for the review pipeline."""

from types import SimpleNamespace
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

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

    def list_files(self):
        return list(self.files)

    def close(self):
        pass


class FakeChatModel(BaseChatModel):
    """A LangChain chat model that hands every call to a FakeLLM, in today's OpenAI format.

    The real LangChainLLM sits in front of it, so tests exercise the LangChain path
    (message conversion, bind_tools, AIMessage mapping) without a network.
    """

    llm: Any

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, tools=None, **kwargs):
        reply = self.llm.chat(convert_to_openai_messages(messages), tools)
        tool_calls = [{"id": tc.id, "name": tc.name, "args": tc.arguments} for tc in reply.tool_calls or []]
        message = AIMessage(content=reply.content or "", tool_calls=tool_calls)
        return ChatResult(generations=[ChatGeneration(message=message)])


def make_session(config, llm=None, reader=None):
    """Build a ReviewSession whose model and file reader are fakes (policy/guard stay real)."""
    review_llm = llm or FakeLLM(content='{"summary": "ok", "issues": []}')

    def factory(cfg, model):
        return FakeChatModel(llm=review_llm)

    return ReviewSession.create(config, inner_reader=reader or StubReader(), chat_model_factory=factory)


def chat_model_factory(responder):
    """Stand-in for the real chat model:
    patch("review_pull_request.session.make_chat_model", chat_model_factory(fn)).

    `fn(messages, tools)` answers every model call; the overview call has tools=None.
    """
    def factory(config, model=None):
        return FakeChatModel(llm=FakeLLM(responder))
    return factory
