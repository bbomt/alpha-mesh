"""Shared setup for every test in eval/tests. The name is pytest's: it loads a file called conftest.py
automatically, before the tests beside it, so the file can't be renamed.

It puts the eval harness on the import path (the harness is standalone scripts, not part of the
review_pull_request package) and gives every test fake judge settings, so no test reads the machine's .env."""

import os
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(EVAL / "scripts"), str(EVAL / "promptfoo"), str(EVAL.parent)]  # EVAL.parent: tests/ fakes


@pytest.fixture(autouse=True)
def _judge_settings(monkeypatch):
    """The loader needs EVAL_JUDGE_* to be set. A live judge test (JUDGE_LIVE=1) uses the real ones from .env."""
    if os.environ.get("JUDGE_LIVE") != "1":
        for var in ("EVAL_JUDGE_MODEL", "EVAL_JUDGE_URL", "EVAL_JUDGE_TOKEN"):
            monkeypatch.setenv(var, "test-value")
        # Empty, not deleted: load_dotenv would refill a deleted one from the machine's .env (a host pin there would
        # then reach every test). Empty counts as unset.
        monkeypatch.setenv("EVAL_JUDGE_PROVIDER", "")
