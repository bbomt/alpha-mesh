"""main() never builds a real GitHubFileReader in tests: it gets an empty stub reader
(no file reads or tree fetch over the network) unless a test patches its own.
It never reaches AWS either: checkpoints go to an InMemorySaver, and run locks to a temp HOME."""

from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from tests.helpers import StubReader


@pytest.fixture(autouse=True)
def _no_github_reader():
    with patch("review_pull_request.review.GitHubFileReader", lambda *a, **k: StubReader()):
        yield


@pytest.fixture(autouse=True)
def _no_aws_and_private_locks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch("review_pull_request.review.make_checkpointer", lambda dry_run=False: InMemorySaver()):
        yield
