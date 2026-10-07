"""Tests for review.py main() entry-point guards."""
import sys
from unittest.mock import MagicMock, patch

from pr_review_performance.config import Config
from pr_review_performance.review import main


def _stub_config():
    return Config(model="m", api_url="http://x", api_token="t")


def _mock_pr_info(head_sha="abc123sha", branch="feature/test"):
    return {"headRefName": branch, "headRefOid": head_sha}


@patch("pr_review_performance.review.load_config", return_value=_stub_config())
@patch("pr_review_performance.review.get_pr_info")
@patch("pr_review_performance.review.parse_pr_url", return_value=("owner", "repo", 42))
@patch("subprocess.run")
def test_exits_with_error_when_no_github_token(mock_subproc, mock_parse, mock_pr_info, mock_cfg, capsys):
    mock_pr_info.return_value = _mock_pr_info()
    mock_subproc.return_value = MagicMock(returncode=1, stdout="")

    sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
    rc = main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "no GitHub token" in err
    assert "gh auth login" in err


@patch("pr_review_performance.review.load_config", return_value=_stub_config())
@patch("pr_review_performance.review.get_pr_description", return_value="")
@patch("pr_review_performance.review.get_pr_info")
@patch("pr_review_performance.review.parse_pr_url", return_value=("owner", "repo", 42))
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
    from pr_review_performance.llm import LLMError

    config = Config(model="bad/model", api_url="https://llm.example.com", api_token=SECRET)
    err = LLMError("API returned 404 with tools, retry also failed: 404 with tools, retry: 404")
    with patch("pr_review_performance.review.load_config", return_value=config), \
         patch("pr_review_performance.review.parse_pr_url", return_value=("owner", "repo", 42)), \
         patch("pr_review_performance.review.get_pr_info", return_value=_mock_pr_info()), \
         patch("pr_review_performance.review.get_pr_description", return_value=""), \
         patch("pr_review_performance.review.get_diff", return_value=DIFF), \
         patch("pr_review_performance.stage1.chat",
               **({"return_value": MagicMock(content="An overview.", tool_calls=[])} if overview_ok else {"side_effect": err})), \
         patch("pr_review_performance.stage2.chat", side_effect=err) as mock_stage2, \
         patch("pr_review_performance.review.post_comment") as mock_post, \
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
