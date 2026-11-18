"""Tests for review.py main() entry-point guards."""
import sys
from unittest.mock import MagicMock, patch

from review_pull_request.config import Config
from review_pull_request.review import main


import pytest

from tests.helpers import FakeLLM, StubReader, chat_model_factory


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


def _raise_404(messages, tools):
    from review_pull_request.llm import LLMError

    raise LLMError("API returned 404")


def _run_main_with_llm_error(capsys, argv_extra=()):
    """Run main() against a PR whose every LLM call raises LLMError(404)."""
    from review_pull_request.llm import LLMError

    config = Config(model="bad/model", api_url="https://llm.example.com", api_token=SECRET)
    err = LLMError("API returned 404 with tools, retry also failed: 404 with tools, retry: 404")
    review_calls = []

    def responder(messages, tools):
        if tools is not None:
            review_calls.append(1)
        raise err

    with patch("review_pull_request.review.load_config", return_value=config), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=DIFF), \
         patch("review_pull_request.session.make_chat_model", chat_model_factory(responder)), \
         patch("review_pull_request.review.post_comment") as mock_post, \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42", *argv_extra]
        rc = main()
    return rc, mock_post, capsys.readouterr(), review_calls


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
    assert "[model] review in review_config.toml" in out.err
    assert "CODE_REVIEW_URL" in out.err


def test_llm_error_output_never_contains_token(capsys):
    _, _, out, _ = _run_main_with_llm_error(capsys)

    assert SECRET not in out.err
    assert SECRET not in out.out


def test_overview_failure_stops_before_any_file_review(capsys):
    rc, _, _, review_calls = _run_main_with_llm_error(capsys)

    assert rc != 0
    assert review_calls == []


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
         patch("review_pull_request.session.make_chat_model", chat_model_factory(_raise_404)), \
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
         patch("review_pull_request.session.make_chat_model", chat_model_factory(_raise_404)), \
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
         patch("review_pull_request.session.make_chat_model", chat_model_factory(_raise_404)), \
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
         patch("review_pull_request.graph.run_overview", return_value="An overview."), \
         patch("review_pull_request.graph.run_file_review", side_effect=file_review), \
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


LAST_REVIEW = f"# AI Code Review (full review of aaaaaaa)\n\n> **Reviewed commit:** `{'a' * 40}`\n"
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


def test_overview_failure_names_the_configured_model(capsys):
    cfg = Config(model="file-model", api_url="https://llm.example.com", api_token=SECRET)
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=DIFF), \
         patch("review_pull_request.session.make_chat_model", chat_model_factory(_raise_404)), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
        main()

    err = capsys.readouterr().err
    assert "model: file-model" in err


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
         patch("review_pull_request.graph.run_overview", return_value="ov"), \
         patch("review_pull_request.graph.run_file_review",
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


def test_excluded_notice_is_printed_so_dry_runs_and_previews_can_see_it(capsys):
    rc, mock_post = _run_main_with_results(lambda *a: None, README_DIFF, exclusions=["**/README.md"],
                                           extra_argv=["--no-post-comment"])

    assert rc == 0
    assert "Pull request not reviewed" in capsys.readouterr().out


def test_excluded_notice_respects_no_output(capsys):
    rc, _ = _run_main_with_results(lambda *a: None, README_DIFF, exclusions=["**/README.md"],
                                   extra_argv=["--no-post-comment", "--no-output"])

    assert rc == 0
    assert "Pull request not reviewed" not in capsys.readouterr().out


def test_the_diff_is_parsed_once_per_run():
    from review_pull_request import safe_diff as safe_diff_module
    from review_pull_request.stage2 import FileReviewResult

    real = safe_diff_module.build_chunks
    with patch("review_pull_request.safe_diff.build_chunks", side_effect=real) as spy:
        _run_main_with_results(lambda c, o, cfg: FileReviewResult(c.file_path, "s", [], 0),
                               README_DIFF + DIFF, exclusions=["**/README.md"])

    assert spy.call_count == 1


def test_excluded_files_never_reach_the_overview_prompt():
    from review_pull_request.stage2 import FileReviewResult

    secret_diff = ("diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n+API_TOKEN=super-secret-value\n")
    cfg = Config(model="m", api_url="http://x", api_token=SECRET, exclusions=["**/.env"])
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=secret_diff + DIFF), \
         patch("review_pull_request.graph.run_overview", return_value="ov") as overview, \
         patch("review_pull_request.graph.run_file_review",
               side_effect=lambda c, o, cf: FileReviewResult(c.file_path, "s", [], 0)), \
         patch("review_pull_request.review.post_comment", return_value=True), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
        main()

    sent = overview.call_args[0][0].text
    assert "super-secret-value" not in sent
    assert "src/a.py" in sent


def test_excluded_notice_escapes_backticks_in_paths():
    from review_pull_request.review import _excluded_notice

    assert "``a`b.py``" in _excluded_notice(["a`b.py"])


def test_excluded_notice_respects_hide_pr_comment(capsys):
    rc, _ = _run_main_with_results(lambda *a: None, README_DIFF, exclusions=["**/README.md"],
                                   extra_argv=["--hide-pr-comment"])

    assert rc == 0
    assert "Pull request not reviewed" not in capsys.readouterr().out


def test_excluded_notice_names_the_builtin_rules_too():
    from review_pull_request.review import _excluded_notice

    assert "built-in" in _excluded_notice([".env"]).lower()


def test_incremental_run_touching_only_excluded_files_does_not_claim_the_pr_is_unreviewed(capsys):
    cfg = Config(model="m", api_url="http://x", api_token=SECRET, exclusions=["**/README.md"])
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.find_last_review_comment",
               return_value={"created_at": "2024-01-15T10:00:00Z", "body": LAST_REVIEW}), \
         patch("review_pull_request.review.get_commits_between",
               return_value=[{"sha": "c" * 40}]), \
         patch("review_pull_request.review.build_incremental_diff", return_value=README_DIFF), \
         patch("review_pull_request.review.post_comment") as mock_post, \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42", "--since-last-review"]
        rc = main()

    assert rc == 0
    mock_post.assert_not_called()
    assert "excluded" in capsys.readouterr().err.lower()


def test_no_unreviewed_notice_when_a_prior_review_exists_even_after_falling_back_to_full_diff(capsys):
    cfg = Config(model="m", api_url="http://x", api_token=SECRET, exclusions=["**/README.md"])
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.find_last_review_comment",
               return_value={"created_at": "2024-01-15T10:00:00Z", "body": LAST_REVIEW}), \
         patch("review_pull_request.review.get_commits_between",
               return_value=[{"sha": "c" * 40}]), \
         patch("review_pull_request.review.build_incremental_diff", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=README_DIFF), \
         patch("review_pull_request.review.post_comment") as mock_post, \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42", "--since-last-review"]
        rc = main()

    assert rc == 0
    mock_post.assert_not_called()


def _since_last_review(last_review_body, head_sha="b" * 40, commits=None, diff=README_DIFF):
    """Run --since-last-review on a README-only change; returns (rc, mocks, stderr)."""
    cfg = Config(model="m", api_url="http://x", api_token=SECRET, exclusions=["**/README.md"])
    with patch("review_pull_request.review.load_config", return_value=cfg), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info(head_sha=head_sha)), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.find_last_review_comment",
               return_value={"created_at": "2024-01-15T10:00:00Z", "body": last_review_body}), \
         patch("review_pull_request.review.get_commits_between", return_value=commits) as between, \
         patch("review_pull_request.review.build_incremental_diff", return_value=diff) as inc_diff, \
         patch("review_pull_request.review.get_diff", return_value=diff) as full_diff, \
         patch("review_pull_request.review.post_comment") as post, \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42", "--since-last-review"]
        rc = main()
    return rc, {"between": between, "inc_diff": inc_diff, "full_diff": full_diff, "post": post}


def test_since_last_review_reviews_from_the_recorded_commit_to_the_head():
    rc, m = _since_last_review(LAST_REVIEW, commits=[{"sha": "c" * 40}])
    assert m["between"].call_args[0][2:4] == ("a" * 40, "b" * 40)
    assert m["inc_diff"].call_args[0][2:4] == ("a" * 40, "b" * 40)
    m["full_diff"].assert_not_called()


def test_since_last_review_with_no_new_commits_does_nothing(capsys):
    rc, m = _since_last_review(LAST_REVIEW, head_sha="a" * 40)
    assert rc == 0
    m["between"].assert_not_called()
    m["full_diff"].assert_not_called()
    m["post"].assert_not_called()
    assert "no new commits since last review (aaaaaaa)" in capsys.readouterr().err.lower()


def test_since_last_review_falls_back_to_full_review_when_the_last_review_has_no_commit(capsys):
    rc, m = _since_last_review("# AI Code Review\nOld-format comment.")
    m["between"].assert_not_called()
    m["full_diff"].assert_called_once()
    assert "full review" in capsys.readouterr().err.lower()


def test_since_last_review_falls_back_to_full_review_when_history_was_rewritten(capsys):
    rc, m = _since_last_review(LAST_REVIEW, commits=None)  # compare: diverged or API error
    m["inc_diff"].assert_not_called()
    m["full_diff"].assert_called_once()
    assert "full review" in capsys.readouterr().err.lower()


def test_report_lists_missing_paths_and_warns_about_symlinks():
    from review_pull_request.stage2 import FileReviewResult

    class LinkedRepo(StubReader):
        symlinks = ["docs/link.md", ".env"]

    def review(chunk, overview, session):
        session.note_missing("src/cli.py", chunk.file_path)
        return FileReviewResult(chunk.file_path, "s", [], 0)

    with patch("review_pull_request.review.GitHubFileReader", lambda *a, **k: LinkedRepo({"src/a.py": ""})):
        rc, mock_post = _run_main_with_results(review, DIFF, exclusions=["**/.env"])

    body = mock_post.call_args[0][0]
    assert rc == 0
    assert "Paths not found" in body and "`src/cli.py` (asked while reviewing `src/a.py`)" in body
    assert "`docs/link.md`" in body and ".env" not in body  # excluded names are not reported either


def test_report_lists_only_the_missing_paths_the_model_did_not_recover_from():
    """PMAT-263, PR #11 shape: a miss followed by a read of the suggested path is noise."""
    asks = ["util.py", "lib/util.py", "nowhere.py"]  # miss (hinted), recovery, miss (no hint)

    def responder(messages, tools):
        if tools is None:  # the overview call
            return MagicMock(content="An overview.", tool_calls=[])
        done = sum(1 for m in messages if m.get("role") == "tool")
        if done < len(asks):
            call = MagicMock(id=str(done), arguments={"path": asks[done], "reason": "r"})
            call.name = "read_file"
            return MagicMock(content=None, tool_calls=[call])
        return MagicMock(content='{"summary": "ok", "issues": []}', tool_calls=[])

    config = Config(model="m", api_url="http://x", api_token=SECRET)
    repo = StubReader({"src/a.py": "", "lib/util.py": "u"})
    with patch("review_pull_request.review.load_config", return_value=config), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=DIFF), \
         patch("review_pull_request.review.GitHubFileReader", lambda *a, **k: repo), \
         patch("review_pull_request.session.make_chat_model", chat_model_factory(responder)), \
         patch("review_pull_request.review.post_comment") as mock_post, \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
        rc = main()

    body = mock_post.call_args[0][0]
    assert rc == 0
    assert "`nowhere.py` (asked while reviewing `src/a.py`)" in body
    assert "`util.py`" not in body


def test_report_notes_an_unavailable_listing_even_if_it_is_first_fetched_for_the_report():
    from review_pull_request.stage2 import FileReviewResult

    class NoListing(StubReader):
        def list_files(self):
            return None

    def review(chunk, overview, session):  # never touches the tree, so the report fetches it
        session.note_missing("src/cli.py", chunk.file_path)
        return FileReviewResult(chunk.file_path, "s", [], 0)

    with patch("review_pull_request.review.GitHubFileReader", lambda *a, **k: NoListing()):
        rc, mock_post = _run_main_with_results(review, DIFF)

    assert "listing was unavailable" in mock_post.call_args[0][0]


def test_dry_run_reviews_every_file_through_the_graph_without_a_real_model_or_posting(capsys):
    from review_pull_request.llm import DRY_RUN_OVERVIEW

    diff = DIFF + DIFF.replace("src/a.py", "src/b.py")
    with patch("review_pull_request.review.load_config", return_value=_stub_config()), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=diff), \
         patch("review_pull_request.session.ChatOpenAI", side_effect=AssertionError("real model built")), \
         patch("review_pull_request.review.post_comment") as mock_post, \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42", "--dry-run"]
        rc = main()

    out, err = capsys.readouterr()
    assert rc == 0
    mock_post.assert_not_called()
    assert DRY_RUN_OVERVIEW in out
    assert "dry-run-model" in out
    assert "**Files reviewed:** 2" in out
    assert "[1/2] reviewing src/a.py" in err and "[2/2] reviewing src/b.py" in err


def test_a_pr_with_many_files_is_reviewed_to_the_end():
    from review_pull_request.stage2 import FileReviewResult

    diff = "".join(DIFF.replace("src/a.py", f"src/f{i}.py") for i in range(30))
    reviewed = []

    def review(chunk, overview, session):
        reviewed.append(chunk.file_path)
        return FileReviewResult(chunk.file_path, "s", [], 0)

    rc, mock_post = _run_main_with_results(review, diff)

    assert rc == 0
    assert reviewed == [f"src/f{i}.py" for i in range(30)]
    assert "**Files reviewed:** 30" in mock_post.call_args[0][0]


def test_a_failed_overview_names_the_overview_as_the_failed_step(capsys):
    _, _, out, _ = _run_main_with_llm_error(capsys)

    assert "LLM API call failed during overview pass" in out.err


def test_a_transient_model_error_mid_review_is_retried_and_the_run_completes(capsys, monkeypatch):
    """PMAT-236: a 503 on a file review is retried; the review is posted and the run succeeds."""
    import httpx
    import openai

    monkeypatch.setattr("review_pull_request.llm._sleep", lambda s: None)
    failed = []

    def responder(messages, tools):
        if tools is None:
            return MagicMock(content="An overview.", tool_calls=[])
        if not failed:
            failed.append(1)
            raise openai.InternalServerError(
                "Error code: 503 - upstream unavailable",
                response=httpx.Response(503, request=httpx.Request("POST", "http://x")), body=None)
        return MagicMock(content='{"summary": "ok", "issues": []}', tool_calls=[])

    config = Config(model="m", api_url="http://x", api_token=SECRET)
    with patch("review_pull_request.review.load_config", return_value=config), \
         patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("review_pull_request.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("review_pull_request.review.get_pr_description", return_value=""), \
         patch("review_pull_request.review.get_diff", return_value=DIFF), \
         patch("review_pull_request.session.make_chat_model", chat_model_factory(responder)), \
         patch("review_pull_request.review.post_comment") as mock_post, \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")):
        sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
        rc = main()

    assert failed == [1]
    assert rc == 0
    mock_post.assert_called_once()
    assert "retrying in" in capsys.readouterr().err
