"""The harness is standalone scripts, not part of the review_pull_request package."""

import os
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(EVAL / "scripts"), str(EVAL / "promptfoo"), str(EVAL.parent)]  # EVAL.parent: tests/ fakes


@pytest.fixture(autouse=True)
def _judge_settings(monkeypatch):
    """The loader needs JUDGE_* to be set. A live judge test (JUDGE_LIVE=1) uses the real ones from .env."""
    if os.environ.get("JUDGE_LIVE") != "1":
        for var in ("EVAL_JUDGE_MODEL", "EVAL_JUDGE_URL", "EVAL_JUDGE_TOKEN"):
            monkeypatch.setenv(var, "test-value")
