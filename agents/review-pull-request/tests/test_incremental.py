"""Tests for incremental.py — --since-last-review logic.

Covers:
- find_last_review_comment: identifying review comments by signature
- get_commits_between: commits after the last reviewed SHA
- build_incremental_diff: diff retrieval (httpx mocking)
- formatter incremental header integration
"""

import pytest
from unittest.mock import MagicMock, patch

from review_pull_request.constants import REVIEW_SIGNATURE
from review_pull_request.incremental import (
    _next_page_url,
    find_last_review_comment,
    get_commits_between,
    build_incremental_diff,
    reviewed_sha,
)
from review_pull_request.formatter import render_report


# ─── Helpers ────────────────────────────────────────────────────────────────

BASE = "a" * 40
HEAD = "b" * 40

def _make_comment(body: str, created_at: str) -> dict:
    return {"body": body, "created_at": created_at, "id": 1}


def _mock_response(json_data, status_code: int = 200, link: str = ""):
    """Build a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = ""
    resp.headers = {"Link": link} if link else {}
    return resp


# ─── _next_page_url ─────────────────────────────────────────────────────────

class TestNextPageUrl:
    def test_returns_none_when_no_link_header(self):
        assert _next_page_url("") is None
        assert _next_page_url(None) is None  # type: ignore[arg-type]

    def test_returns_none_when_no_next_rel(self):
        link = '<https://api.github.com/repos/o/r/issues/1/comments?page=1>; rel="prev"'
        assert _next_page_url(link) is None

    def test_extracts_next_url(self):
        link = (
            '<https://api.github.com/repos/o/r/issues/1/comments?page=1>; rel="prev", '
            '<https://api.github.com/repos/o/r/issues/1/comments?page=3>; rel="next"'
        )
        result = _next_page_url(link)
        assert result == "https://api.github.com/repos/o/r/issues/1/comments?page=3"

    def test_extracts_next_when_only_rel(self):
        link = '<https://api.github.com/page2>; rel="next"'
        assert _next_page_url(link) == "https://api.github.com/page2"


# ─── find_last_review_comment ────────────────────────────────────────────────

class TestFindLastReviewComment:
    def test_returns_none_when_no_comments(self):
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response([])
            result = find_last_review_comment("owner", "repo", 1, "token")
        assert result is None

    def test_returns_none_when_no_review_comments(self):
        comments = [
            _make_comment("Just a regular comment", "2024-01-01T10:00:00Z"),
            _make_comment("Another comment without signature", "2024-01-02T10:00:00Z"),
        ]
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(comments)
            result = find_last_review_comment("owner", "repo", 1, "token")
        assert result is None

    def test_returns_review_comment_by_signature(self):
        comments = [
            _make_comment(f"{REVIEW_SIGNATURE}\nSome findings.", "2024-01-15T10:00:00Z"),
        ]
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(comments)
            result = find_last_review_comment("owner", "repo", 1, "token")
        assert result is not None
        assert REVIEW_SIGNATURE in result["body"]

    def test_returns_most_recent_review_comment(self):
        comments = [
            _make_comment(f"{REVIEW_SIGNATURE}\nOlder review.", "2024-01-10T09:00:00Z"),
            _make_comment("Not a review comment.", "2024-01-12T09:00:00Z"),
            _make_comment(f"{REVIEW_SIGNATURE}\nNewer review.", "2024-01-15T10:00:00Z"),
        ]
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(comments)
            result = find_last_review_comment("owner", "repo", 1, "token")
        assert result is not None
        assert result["created_at"] == "2024-01-15T10:00:00Z"
        assert "Newer review." in result["body"]

    def test_matches_incremental_review_header(self):
        """Incremental review headers should also be detected as review comments."""
        body = f"{REVIEW_SIGNATURE} (incremental — 3 commit(s) since last review)\nFindings."
        comments = [_make_comment(body, "2024-02-01T08:00:00Z")]
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(comments)
            result = find_last_review_comment("owner", "repo", 1, "token")
        assert result is not None

    def test_returns_none_on_api_error(self):
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response({}, status_code=403)
            result = find_last_review_comment("owner", "repo", 1, "token")
        assert result is None

    def test_returns_none_on_network_exception(self):
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.side_effect = Exception("connection refused")
            result = find_last_review_comment("owner", "repo", 1, "token")
        assert result is None

    def test_paginates_to_find_review_comment(self):
        """Review comment on page 2 is still found via Link pagination."""
        page1_comment = _make_comment("No signature here", "2024-01-10T10:00:00Z")
        page2_comment = _make_comment(f"{REVIEW_SIGNATURE}\nFindings.", "2024-01-15T10:00:00Z")

        page1_resp = MagicMock()
        page1_resp.status_code = 200
        page1_resp.json.return_value = [page1_comment]
        page1_resp.headers = {
            "Link": '<https://api.github.com/page2>; rel="next"'
        }

        page2_resp = MagicMock()
        page2_resp.status_code = 200
        page2_resp.json.return_value = [page2_comment]
        page2_resp.headers = {}

        with patch("review_pull_request.incremental.httpx.get", side_effect=[page1_resp, page2_resp]):
            result = find_last_review_comment("owner", "repo", 1, "token")

        assert result is not None
        assert "Findings." in result["body"]


# ─── get_commits_between ─────────────────────────────────────────────────────

class TestGetCommitsBetween:
    def _compare(self, status, shas):
        return _mock_response({"status": status, "commits": [{"sha": x} for x in shas]})

    def test_returns_commits_when_head_is_ahead_of_the_reviewed_commit(self):
        with patch("review_pull_request.incremental.httpx.get",
                   return_value=self._compare("ahead", ["c1", "c2"])) as mock_get:
            result = get_commits_between("o", "r", "base", "head", "token")
        assert [c["sha"] for c in result] == ["c1", "c2"]
        assert "base...head" in mock_get.call_args[0][0]

    def test_returns_empty_when_nothing_is_new(self):
        with patch("review_pull_request.incremental.httpx.get",
                   return_value=self._compare("identical", [])):
            assert get_commits_between("o", "r", "base", "head", "token") == []

    def test_returns_none_when_history_was_rewritten(self):
        # Force-push or rebase: the reviewed commit is no longer an ancestor of the head.
        with patch("review_pull_request.incremental.httpx.get",
                   return_value=self._compare("diverged", ["c1"])):
            assert get_commits_between("o", "r", "base", "head", "token") is None

    def test_returns_none_on_api_error(self):
        with patch("review_pull_request.incremental.httpx.get",
                   return_value=_mock_response({}, status_code=404)):
            assert get_commits_between("o", "r", "base", "head", "token") is None

    def test_returns_none_on_exception(self):
        with patch("review_pull_request.incremental.httpx.get", side_effect=Exception("timeout")):
            assert get_commits_between("o", "r", "base", "head", "token") is None


# ─── reviewed_sha ────────────────────────────────────────────────────────────

class TestReviewedSha:
    def test_reads_the_commit_a_rendered_report_records(self):
        report = render_report("overview", [], [], MockConfig(), head_sha=HEAD)
        assert reviewed_sha(report) == HEAD

    def test_old_comments_without_a_recorded_commit_return_none(self):
        assert reviewed_sha(f"{REVIEW_SIGNATURE}\nSome findings.") is None

    def test_a_commit_quoted_further_down_the_comment_is_ignored(self):
        body = f"{REVIEW_SIGNATURE}\n\noverview\n\n> **Reviewed commit:** `{HEAD}`\n"
        assert reviewed_sha(body) is None


# ─── build_incremental_diff ──────────────────────────────────────────────────

class TestBuildIncrementalDiff:
    def test_returns_diff_on_success(self):
        sample_diff = "diff --git a/file.py b/file.py\n+new line\n"
        resp = MagicMock()
        resp.status_code = 200
        resp.text = sample_diff
        with patch("review_pull_request.incremental.httpx.get", return_value=resp):
            result = build_incremental_diff("owner", "repo", "base_sha", "head_sha", "token")
        assert result == sample_diff

    def test_returns_empty_string_on_api_error(self):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = ""
        with patch("review_pull_request.incremental.httpx.get", return_value=resp):
            result = build_incremental_diff("owner", "repo", "base", "head", "token")
        assert result == ""

    def test_returns_empty_string_on_exception(self):
        with patch("review_pull_request.incremental.httpx.get", side_effect=Exception("timeout")):
            result = build_incremental_diff("owner", "repo", "base", "head", "token")
        assert result == ""

    def test_uses_three_dot_compare_url(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "diff"
        with patch("review_pull_request.incremental.httpx.get", return_value=resp) as mock_get:
            build_incremental_diff("owner", "repo", "abc123", "def456", "token")
        call_url = mock_get.call_args[0][0]
        assert "abc123...def456" in call_url


# ─── formatter integration ───────────────────────────────────────────────────

class MockConfig:
    model = "test-model"
    overview_model = ""
    base_branch = "master"

    @property
    def effective_overview_model(self):
        return self.overview_model or self.model


class TestFormatterIncrementalHeader:
    def test_full_review_header(self):
        report = render_report("overview", [], [], MockConfig(), head_sha=HEAD)
        assert report.split("\n")[0] == f"{REVIEW_SIGNATURE} (full review of bbbbbbb)"
        assert f"> **Reviewed commit:** `{HEAD}`" in report

    def test_incremental_review_header(self):
        incremental_info = {"base_sha": BASE, "commits": ["c" * 40, "d" * 40, "e" * 40]}
        report = render_report("overview", [], [], MockConfig(), incremental_info=incremental_info,
                               head_sha=HEAD)
        assert report.split("\n")[0] == (
            f"{REVIEW_SIGNATURE} (since last review: aaaaaaa..bbbbbbb, 3 new commit(s))"
        )
        assert "> **New commits:** `ccccccc` · `ddddddd` · `eeeeeee`" in report
        assert reviewed_sha(report) == HEAD

    def test_incremental_header_contains_signature(self):
        """Incremental headers must still be detected by REVIEW_SIGNATURE search."""
        incremental_info = {"base_sha": BASE, "commits": ["c" * 40]}
        report = render_report("overview", [], [], MockConfig(), incremental_info=incremental_info)
        assert REVIEW_SIGNATURE in report

    def test_attribution_footer_present(self):
        report = render_report("overview", [], [], MockConfig())
        assert "mesh review-pull-request" in report
        assert "test-model" in report


def test_not_reviewed_notice_is_not_mistaken_for_a_review():
    from review_pull_request.review import _excluded_notice

    notice = _excluded_notice(["README.md"])

    assert REVIEW_SIGNATURE not in notice


def test_a_review_marker_quoted_inside_a_report_body_is_not_a_baseline():
    quoted = _make_comment("# Partial pull request review\n\n> the diff mentions # AI Code Review here",
                           "2024-01-15T10:00:00Z")
    with patch("review_pull_request.incremental._paginate_get", return_value=[quoted]):
        assert find_last_review_comment("o", "r", 1, "tok") is None
