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


def test_post_comment_is_true_even_if_the_success_body_is_not_json():
    from review_pull_request.github import post_comment

    resp = MagicMock(status_code=201, text="ok")
    resp.json.side_effect = ValueError("not json")
    with patch("review_pull_request.github._get_github_token", return_value="t"), \
         patch("review_pull_request.github.httpx.post", return_value=resp):
        assert post_comment("body", "https://github.com/o/r/pull/1", None) is True


def test_oversized_report_is_truncated_to_githubs_comment_limit():
    from review_pull_request.github import MAX_COMMENT_CHARS, post_comment

    resp = MagicMock(status_code=201, text="ok")
    resp.json.return_value = {"html_url": "u"}
    with patch("review_pull_request.github._get_github_token", return_value="t"), \
         patch("review_pull_request.github.httpx.post", return_value=resp) as post:
        assert post_comment("x" * 200_000, "https://github.com/o/r/pull/1", None) is True

    sent = post.call_args.kwargs["json"]["body"]
    assert len(sent) <= MAX_COMMENT_CHARS
    assert "truncated" in sent[-300:].lower()


def test_truncation_cuts_the_findings_and_keeps_the_header_and_coverage_disclosures():
    from types import SimpleNamespace

    from review_pull_request.formatter import render_report
    from review_pull_request.github import MAX_COMMENT_CHARS, post_comment
    from review_pull_request.stage2 import Issue

    issues = [Issue(file=f"src/f{i}.py", line=1, severity="low", comment="c" * 2_000, suggestion=None)
              for i in range(60)]
    config = SimpleNamespace(base_branch="master", model="m")
    report = render_report("overview", issues, [("src/a.py", "s"), ("src/b.py", "s")], config,
                           incomplete=["src/b.py"], excluded=[".env"], head_sha="a" * 40)
    assert len(report) > MAX_COMMENT_CHARS

    resp = MagicMock(status_code=201, text="ok")
    resp.json.return_value = {"html_url": "u"}
    with patch("review_pull_request.github._get_github_token", return_value="t"), \
         patch("review_pull_request.github.httpx.post", return_value=resp) as post:
        assert post_comment(report, "https://github.com/o/r/pull/1", None) is True

    sent = post.call_args.kwargs["json"]["body"]
    assert len(sent) <= MAX_COMMENT_CHARS
    assert sent.startswith("# Partial pull request review")
    assert "Reviewed commit" in sent
    assert "truncated" in sent.lower()
    assert "## ⚠️ Not fully reviewed" in sent and "`src/b.py`" in sent.split("Not fully reviewed")[1]
    assert "## Excluded from review" in sent and "`.env`" in sent.split("Excluded from review")[1]
