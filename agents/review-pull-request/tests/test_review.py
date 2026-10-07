"""Tests for review.py main() entry-point guards."""
import sys
from unittest.mock import MagicMock, patch

from review_pull_request.config import Config
from review_pull_request.review import main


import pytest


@pytest.fixture(autouse=True)
def _restore_argv():
    saved = sys.argv[:]
    yield
    sys.argv = saved


def _stub_config():
    return Config(model="m", api_url="http://x", api_token="t")


def _mock_pr_info(head_sha="abc123sha", branch="feature/test"):
    return {"headRefName": branch, "headRefOid": head_sha}


@patch("review_pull_request.review.load_config", return_value=_stub_config())
@patch("review_pull_request.review.get_pr_description", return_value="")
@patch("review_pull_request.review.get_pr_info")
@patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42))
@patch("subprocess.run")
def test_exits_with_error_when_no_github_token(mock_subproc, mock_parse, mock_pr_info, mock_desc, mock_cfg, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock_pr_info.return_value = _mock_pr_info()
    mock_subproc.return_value = MagicMock(returncode=1, stdout="")

    sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
    rc = main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "no GitHub token" in err
    assert "gh auth login" in err


@patch("review_pull_request.review.load_config", return_value=_stub_config())
@patch("review_pull_request.review.get_pr_description", return_value="")
@patch("review_pull_request.review.get_pr_info")
@patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42))
@patch("subprocess.run")
def test_exits_with_error_when_no_head_sha(mock_subproc, mock_parse, mock_pr_info, mock_desc, mock_cfg, capsys):
    mock_pr_info.return_value = {"headRefName": "feature/test", "headRefOid": ""}
    mock_subproc.return_value = MagicMock(returncode=0, stdout="mytoken")

    sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
    rc = main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "head SHA" in err


# --- LLM API failures must fail noisily (PMAT-229) ---

SECRET = "secret-token-123"
DIFF = (
    "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
    "@@ -1 +1,2 @@\n+one\n+two\n"
)


def _run_main_with_llm_error(capsys, argv_extra=(), overview_ok=False):
    """Run main() against a PR whose every LLM call raises LLMError(404)."""
    from review_pull_request.llm import LLMError

    config = Config(model="bad/model", api_url="https://llm.example.com", api_token=SECRET)
    err = LLMError("API returned 404 with tools, retry also failed: 404 with tools, retry: 404")
    with patch("review_pull_request.review.load_config", return_value=config), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=DIFF), \
         patch("review_pull_request.stage1.chat",
               **({"return_value": MagicMock(content="An overview.", tool_calls=[])} if overview_ok else {"side_effect": err})), \
         patch("review_pull_request.stage2.chat", side_effect=err) as mock_stage2, \
         patch("review_pull_request.review.post_comment") as mock_post, \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42", *argv_extra]
        rc = main()
    return rc, mock_post, capsys.readouterr(), mock_stage2


def test_llm_404_aborts_with_nonzero_exit_and_no_post(capsys):
    rc, mock_post, out, _ = _run_main_with_llm_error(capsys)

    assert rc != 0
    mock_post.assert_not_called()
    assert "No issues found" not in out.out


def test_llm_error_message_names_status_model_host_and_hint(capsys):
    rc, _, out, _ = _run_main_with_llm_error(capsys)

    assert "404" in out.err
    assert "bad/model" in out.err
    assert "llm.example.com" in out.err
    assert "CODE_REVIEW_MODEL" in out.err
    assert "CODE_REVIEW_URL" in out.err


def test_llm_error_output_never_contains_token(capsys):
    _, _, out, _ = _run_main_with_llm_error(capsys)

    assert SECRET not in out.err
    assert SECRET not in out.out


def test_overview_failure_stops_before_any_file_review(capsys):
    rc, _, _, mock_stage2 = _run_main_with_llm_error(capsys)

    assert rc != 0
    mock_stage2.assert_not_called()


def test_file_review_failure_after_good_overview_aborts_and_does_not_post(capsys):
    rc, mock_post, out, _ = _run_main_with_llm_error(capsys, overview_ok=True)

    assert rc != 0
    mock_post.assert_not_called()
    assert "No issues found" not in out.out
    assert "src/a.py" in out.err


# --- startup logging of model and URL ---

@patch("review_pull_request.review.load_config")
@patch("review_pull_request.review.get_pr_description", return_value="")
@patch("review_pull_request.review.get_pr_info")
@patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42))
@patch("subprocess.run")
def test_logs_model_and_url_at_startup_without_token(mock_subproc, mock_parse, mock_pr_info, mock_desc, mock_cfg, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock_cfg.return_value = Config(model="my/model", api_url="https://llm.example.com/api", api_token=SECRET)
    mock_pr_info.return_value = _mock_pr_info()
    mock_subproc.return_value = MagicMock(returncode=1, stdout="")

    sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
    main()

    err = capsys.readouterr().err
    assert "my/model" in err
    assert "https://llm.example.com/api" in err
    assert SECRET not in err


def test_startup_banner_and_error_never_print_url_credentials(capsys):
    from review_pull_request.llm import LLMError

    url = "https://user:pw123@llm.example.com:8443/api?key=abc999"
    cfg = Config(model="m", api_url=url, api_token=SECRET)
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=DIFF), \
         patch("review_pull_request.stage1.chat", side_effect=LLMError("API returned 404")), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
        main()

    err = capsys.readouterr().err
    assert "llm.example.com" in err
    assert "pw123" not in err
    assert "abc999" not in err


def test_read_cache_is_cleared_when_main_exits(capsys):
    from review_pull_request.llm import LLMError

    cfg = Config(model="m", api_url="http://x", api_token=SECRET)
    cfg.read_cache["pyproject.toml"] = "FILE BODY"
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=DIFF), \
         patch("review_pull_request.stage1.chat", side_effect=LLMError("API returned 404")), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
        main()

    assert cfg.read_cache == {}


def test_safe_url_survives_a_malformed_port():
    from review_pull_request.review import _safe_url

    assert "abc" not in _safe_url("https://host:abc/v1") or _safe_url("https://host:abc/v1") == "(invalid URL)"
    assert _safe_url("https://host:99999/v1") in ("(invalid URL)", "https://host/v1")


def test_verbose_output_never_prints_url_credentials(capsys):
    from review_pull_request.llm import LLMError

    cfg = Config(model="m", api_url="https://user:pw123@llm.example.com/api?key=abc999",
                 api_token=SECRET, verbose=True)
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=DIFF), \
         patch("review_pull_request.stage1.chat", side_effect=LLMError("API returned 404")), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42", "--verbose"]
        main()

    err = capsys.readouterr().err
    assert "pw123" not in err
    assert "abc999" not in err


def test_github_token_env_var_is_accepted_when_gh_has_no_token(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    with patch("review_pull_request.review.load_config", return_value=_stub_config()), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value={"headRefName": "b", "headRefOid": ""}), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
        main()

    assert "no GitHub token" not in capsys.readouterr().err  # got past the token check (fails later on head SHA)


# --- incomplete files, all-excluded diffs, error reporting (pass 6) ---

def _run_main_with_results(file_review, diff, exclusions=(), extra_argv=()):
    cfg = Config(model="m", api_url="http://x", api_token=SECRET, exclusions=list(exclusions))
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=diff), \
         patch("review_pull_request.review.run_overview", return_value="An overview."), \
         patch("review_pull_request.review.run_file_review", side_effect=file_review), \
         patch("review_pull_request.review.post_comment") as mock_post, \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42", *extra_argv]
        rc = main()
    return rc, mock_post


def test_incomplete_file_reviews_are_reported_and_fail_the_run(capsys):
    from review_pull_request.stage2 import FileReviewResult

    def review(chunk, overview, config):
        return FileReviewResult(file_path=chunk.file_path, summary="Could not parse model response.",
                                issues=[], tool_calls_made=0, incomplete=True)

    rc, mock_post = _run_main_with_results(review, DIFF)

    body = mock_post.call_args[0][0]
    assert "No issues found. ✅" not in body
    assert "src/a.py" in body
    assert rc != 0
    assert "not fully reviewed" in capsys.readouterr().err.lower()


README_DIFF = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n+x\n"


def test_all_files_excluded_is_announced_on_the_pr_not_silently_skipped(capsys):
    rc, mock_post = _run_main_with_results(lambda *a: None, README_DIFF, exclusions=["**/README.md"])

    assert rc == 0
    mock_post.assert_called_once()
    body = mock_post.call_args[0][0]
    assert "excluded" in body.lower()
    assert "README.md" in body
    assert "excluded" in capsys.readouterr().err.lower()


def test_all_files_excluded_posts_nothing_with_no_post_comment():
    rc, mock_post = _run_main_with_results(lambda *a: None, README_DIFF, exclusions=["**/README.md"],
                                           extra_argv=["--no-post-comment"])

    assert rc == 0
    mock_post.assert_not_called()


def test_safe_url_survives_unparseable_urls():
    from review_pull_request.review import _safe_url

    assert _safe_url("http://[::1") == "(invalid URL)"


def test_overview_failure_names_the_overview_model(capsys):
    from review_pull_request.llm import LLMError

    cfg = Config(model="file-model", api_url="https://llm.example.com", api_token=SECRET,
                 overview_model="overview-model")
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=DIFF), \
         patch("review_pull_request.stage1.chat", side_effect=LLMError("API returned 404")), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
        main()

    err = capsys.readouterr().err
    assert "model: overview-model" in err


def test_failed_post_of_the_excluded_notice_fails_the_run(capsys):
    cfg = Config(model="m", api_url="http://x", api_token=SECRET, exclusions=["**/README.md"])
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=README_DIFF), \
         patch("review_pull_request.review.post_comment", return_value=False), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
        rc = main()

    assert rc != 0
    assert "could not post" in capsys.readouterr().err.lower()


def test_failed_post_of_a_normal_report_fails_the_run(capsys):
    cfg = Config(model="m", api_url="http://x", api_token=SECRET)
    from review_pull_request.stage2 import FileReviewResult

    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=DIFF), \
         patch("review_pull_request.review.run_overview", return_value="ov"), \
         patch("review_pull_request.review.run_file_review",
               side_effect=lambda c, o, cfg: FileReviewResult(c.file_path, "s", [], 0)), \
         patch("review_pull_request.review.post_comment", return_value=False), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
        rc = main()

    assert rc != 0


def test_excluded_notice_caps_a_very_long_file_list():
    from review_pull_request.review import _excluded_notice

    notice = _excluded_notice([f"vendor/f{i}.py" for i in range(500)])

    assert "and 450 more" in notice
    assert len(notice) < 10_000


def test_mixed_pr_report_lists_the_excluded_files():
    from review_pull_request.stage2 import FileReviewResult

    def review(chunk, overview, config):
        return FileReviewResult(chunk.file_path, "s", [], 0)

    rc, mock_post = _run_main_with_results(review, README_DIFF + DIFF, exclusions=["**/README.md"])

    body = mock_post.call_args[0][0]
    assert "Excluded from review" in body
    assert "README.md" in body
