"""main() never builds a real GitHubFileReader in tests: it gets an empty stub reader
(no file reads or tree fetch over the network) unless a test patches its own."""

from unittest.mock import patch

import pytest

from tests.helpers import StubReader


@pytest.fixture(autouse=True)
def _no_github_reader():
    with patch("review_pull_request.review.GitHubFileReader", lambda *a, **k: StubReader()):
        yield
