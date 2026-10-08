"""Tests for stage1 overview pass."""

from types import SimpleNamespace

import pytest

from review_pull_request.config import Config
from review_pull_request.llm import LLMError
from review_pull_request.policy import PathPolicy
from review_pull_request.safe_diff import SafeDiff
from review_pull_request.stage1 import run_overview
from tests.helpers import FakeLLM, make_session

DIFF = SafeDiff.from_raw("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+x\n", PathPolicy([]))


def _config(**kw):
    return Config(model="file-model", api_url="http://x", api_token="t", **kw)


def test_run_overview_raises_on_llm_error():
    """A failed overview call must propagate, not return 'Overview not available'."""
    def raiser(messages, tools):
        raise LLMError("API returned 404")

    session = make_session(_config(), overview_llm=FakeLLM(raiser))

    with pytest.raises(LLMError):
        run_overview(DIFF, "desc", session)


def test_run_overview_uses_the_overview_client_not_the_review_client():
    review_llm = FakeLLM(content="from the review model")
    overview_llm = FakeLLM(content="from the overview model")
    session = make_session(_config(overview_model="overview-model"), llm=review_llm, overview_llm=overview_llm)

    assert run_overview(DIFF, "desc", session) == "from the overview model"
    assert review_llm.calls == []


def test_run_overview_prompt_contains_the_safe_diff_text():
    llm = FakeLLM(content="ok")
    session = make_session(_config(), overview_llm=llm)

    run_overview(DIFF, "desc", session)

    assert "+x" in llm.calls[0][0][0]["content"]
