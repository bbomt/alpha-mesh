"""Tests for incremental.py — --since-last-review logic.

Covers:
- find_last_review_comment: identifying review comments by signature
- get_commits_since: filtering commits by timestamp
- build_incremental_diff: diff retrieval (httpx mocking)
- formatter incremental header integration
"""

import pytest
from unittest.mock import MagicMock, patch

from review_pull_request.constants import REVIEW_SIGNATURE
from review_pull_request.incremental import (
    _parse_timestamp,
    _next_page_url,
    find_last_review_comment,
    get_commits_since,
    build_incremental_diff,
)
from review_pull_request.formatter import render_report


# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_comment(body: str, created_at: str) -> dict:
    return {"body": body, "created_at": created_at, "id": 1}


def _make_commit(sha: str, date: str, parents: list[dict] | None = None) -> dict:
    return {
        "sha": sha,
        "commit": {
            "committer": {"date": date},
            "author": {"date": date},
        },
        "parents": parents or [],
    }


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


# ─── _parse_timestamp ────────────────────────────────────────────────────────

class TestParseTimestamp:
    def test_parses_z_suffix(self):
        dt = _parse_timestamp("2024-01-15T10:30:00Z")
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 10

    def test_parses_plus_offset(self):
        dt = _parse_timestamp("2024-01-15T10:30:00+00:00")
        assert dt.year == 2024
        assert dt.hour == 10

    def test_timestamps_are_comparable(self):
        earlier = _parse_timestamp("2024-01-15T10:00:00Z")
        later = _parse_timestamp("2024-01-15T11:00:00Z")
        assert later > earlier


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


# ─── get_commits_since ───────────────────────────────────────────────────────

class TestGetCommitsSince:
    def test_returns_empty_when_no_commits(self):
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response([])
            result = get_commits_since("owner", "repo", 1, "2024-01-01T00:00:00Z", "token")
        assert result == []

    def test_filters_out_old_commits(self):
        commits = [
            _make_commit("abc1", "2024-01-10T08:00:00Z"),
            _make_commit("abc2", "2024-01-12T08:00:00Z"),
        ]
        since = "2024-01-15T00:00:00Z"
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(commits)
            result = get_commits_since("owner", "repo", 1, since, "token")
        assert result == []

    def test_returns_new_commits_only(self):
        commits = [
            _make_commit("old1", "2024-01-10T08:00:00Z"),
            _make_commit("new1", "2024-01-20T08:00:00Z"),
            _make_commit("new2", "2024-01-21T08:00:00Z"),
        ]
        since = "2024-01-15T00:00:00Z"
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(commits)
            result = get_commits_since("owner", "repo", 1, since, "token")
        assert len(result) == 2
        assert result[0]["sha"] == "new1"
        assert result[1]["sha"] == "new2"

    def test_returns_commits_in_chronological_order(self):
        # API may return in any order; we sort by date
        commits = [
            _make_commit("c3", "2024-01-25T08:00:00Z"),
            _make_commit("c1", "2024-01-16T08:00:00Z"),
            _make_commit("c2", "2024-01-20T08:00:00Z"),
        ]
        since = "2024-01-15T00:00:00Z"
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(commits)
            result = get_commits_since("owner", "repo", 1, since, "token")
        assert [c["sha"] for c in result] == ["c1", "c2", "c3"]

    def test_excludes_commit_exactly_at_cutoff(self):
        """Commit at exactly the cutoff timestamp is NOT included (strictly after)."""
        commits = [_make_commit("sha1", "2024-01-15T10:00:00Z")]
        since = "2024-01-15T10:00:00Z"
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(commits)
            result = get_commits_since("owner", "repo", 1, since, "token")
        assert result == []

    def test_returns_none_on_api_error(self):
        """API error is distinguished from 'no new commits' by returning None."""
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response({}, status_code=500)
            result = get_commits_since("owner", "repo", 1, "2024-01-01T00:00:00Z", "token")
        assert result is None

    def test_returns_none_on_network_exception(self):
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.side_effect = Exception("timeout")
            result = get_commits_since("owner", "repo", 1, "2024-01-01T00:00:00Z", "token")
        assert result is None

    def test_skips_commits_without_date(self):
        """Commits missing date fields are skipped."""
        commits = [
            {"sha": "no_date", "commit": {}, "parents": []},
            _make_commit("with_date", "2024-01-20T08:00:00Z"),
        ]
        since = "2024-01-15T00:00:00Z"
        with patch("review_pull_request.incremental.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(commits)
            result = get_commits_since("owner", "repo", 1, since, "token")
        assert len(result) == 1
        assert result[0]["sha"] == "with_date"


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
        report = render_report("overview", [], [], MockConfig())
        assert report.startswith(f"{REVIEW_SIGNATURE}\n")
        assert "incremental" not in report.split("\n")[0]

    def test_incremental_review_header(self):
        incremental_info = {"commit_count": 3, "since_timestamp": "2024-01-15T10:00:00Z"}
        report = render_report("overview", [], [], MockConfig(), incremental_info=incremental_info)
        first_line = report.split("\n")[0]
        assert REVIEW_SIGNATURE in first_line
        assert "incremental" in first_line
        assert "3 commit(s)" in first_line

    def test_incremental_header_contains_signature(self):
        """Incremental headers must still be detected by REVIEW_SIGNATURE search."""
        incremental_info = {"commit_count": 1, "since_timestamp": "2024-01-15T10:00:00Z"}
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
