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


def test_missing_gh_binary_falls_back_to_env_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    with patch("review_pull_request.github.subprocess.run", side_effect=FileNotFoundError("gh")):
        assert _get_github_token() == "from-env"


def test_missing_gh_binary_and_no_env_token_returns_none(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with patch("review_pull_request.github.subprocess.run", side_effect=FileNotFoundError("gh")):
        assert _get_github_token() is None


def test_gh_auth_token_call_has_a_timeout(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with patch("review_pull_request.github.subprocess.run", return_value=MagicMock(returncode=0, stdout="t")) as run:
        _get_github_token()

    assert run.call_args.kwargs.get("timeout") == 10


def test_a_hung_gh_falls_back_to_the_env_token(monkeypatch):
    import subprocess

    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    with patch("review_pull_request.github.subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 10)):
        assert _get_github_token() == "from-env"


def _post(status, token="t"):
    from review_pull_request.github import post_comment

    resp = MagicMock(status_code=status, text="x")
    resp.json.return_value = {"html_url": "https://example/c"}
    with patch("review_pull_request.github._get_github_token", return_value=token), \
         patch("review_pull_request.github.httpx.post", return_value=resp):
        return post_comment("body", "https://github.com/o/r/pull/1", None)


def test_post_comment_returns_true_on_success():
    assert _post(201) is True


def test_post_comment_returns_false_on_api_failure():
    assert _post(422) is False


def test_post_comment_returns_false_without_a_token():
    assert _post(201, token=None) is False
