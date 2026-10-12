"""No test reaches GitHub: main() gets an empty stub reader unless a test patches its own."""

from unittest.mock import patch

import pytest

from tests.helpers import StubReader


@pytest.fixture(autouse=True)
def _no_github_reader():
    with patch("review_pull_request.review.GitHubFileReader", lambda *a, **k: StubReader()):
        yield
