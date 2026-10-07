"""Tests for stage1 overview pass."""

import pytest
from unittest.mock import patch

from pr_review_performance.llm import LLMError
from pr_review_performance.stage1 import run_overview


class _Config:
    model = "m"
    api_url = "https://example.com"
    api_token = "t"


def test_run_overview_raises_on_llm_error():
    """A failed overview call must propagate, not return 'Overview not available'."""
    with patch("pr_review_performance.stage1.chat", side_effect=LLMError("API returned 404")):
        with pytest.raises(LLMError):
            run_overview("diff", "desc", _Config())
