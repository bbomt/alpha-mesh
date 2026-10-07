"""Tests for the GitHub token lookup."""

from unittest.mock import MagicMock, patch

from review_pull_request.github import _get_github_token


def _gh(returncode, stdout=""):
    return patch("review_pull_request.github.subprocess.run", return_value=MagicMock(returncode=returncode, stdout=stdout))


def test_gh_auth_token_wins(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    with _gh(0, "from-gh\n"):
        assert _get_github_token() == "from-gh"


def test_falls_back_to_github_token_env_var(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    with _gh(1):
        assert _get_github_token() == "from-env"


def test_none_when_neither_is_available(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with _gh(1):
        assert _get_github_token() is None
