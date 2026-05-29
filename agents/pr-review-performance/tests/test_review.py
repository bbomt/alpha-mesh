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
@patch("pr_review_performance.review.get_pr_info")
@patch("pr_review_performance.review.parse_pr_url", return_value=("owner", "repo", 42))
@patch("subprocess.run")
def test_exits_with_error_when_no_head_sha(mock_subproc, mock_parse, mock_pr_info, mock_cfg, capsys):
    mock_pr_info.return_value = {"headRefName": "feature/test", "headRefOid": ""}
    mock_subproc.return_value = MagicMock(returncode=0, stdout="mytoken")

    sys.argv = ["review", "https://github.com/owner/repo/pull/42"]
    rc = main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "head SHA" in err
