"""Tests for stage1 overview pass."""

import pytest
from unittest.mock import patch

from review_pull_request.llm import LLMError
from review_pull_request.stage1 import run_overview


class _Config:
    model = "m"
    api_url = "https://example.com"
    api_token = "t"
    effective_overview_model = "m"


def test_run_overview_raises_on_llm_error():
    """A failed overview call must propagate, not return 'Overview not available'."""
    with patch("review_pull_request.stage1.chat", side_effect=LLMError("API returned 404")):
        with pytest.raises(LLMError):
            run_overview("diff", "desc", _Config())


def test_run_overview_calls_llm_with_the_overview_model():
    from types import SimpleNamespace
    from review_pull_request.config import Config

    cfg = Config(model="file-model", api_url="http://x", api_token="t", overview_model="overview-model")
    seen = {}

    def fake_chat(messages, tools, config):
        seen["model"] = config.model
        return SimpleNamespace(content="ok", tool_calls=[])

    with patch("review_pull_request.stage1.chat", side_effect=fake_chat):
        run_overview("diff", "desc", cfg)

    assert seen["model"] == "overview-model"
    assert cfg.model == "file-model"  # caller's config is not mutated
