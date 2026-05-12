"""Unit tests for context_builder module."""

import pytest
from pr_review.context_builder import build_review_prompt


class TestBuildReviewPrompt:
    """Tests for build_review_prompt function."""

    @pytest.fixture
    def sample_pr(self):
        """Sample PR metadata."""
        return {
            "title": "Add login feature",
            "body": "This PR adds login functionality",
            "head": {"ref": "feature/login"},
            "base": {"ref": "main"},
            "user": {"login": "johndoe"},
            "html_url": "https://github.com/org/repo/pull/123",
            "state": "open",
        }

    @pytest.fixture
    def sample_diff(self):
        """Sample unified diff."""
        return """diff --git a/src/main.py b/src/main.py
index 1234..5678 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,3 +1,5 @@
+def login():
+    pass
"""

    @pytest.fixture
    def sample_files(self):
        """Sample changed files."""
        return [
            {"filename": "src/main.py", "status": "modified"},
        ]

    def test_prompt_contains_pr_title(self, sample_pr):
        """Test that prompt contains PR title."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff="",
            changed_files=[],
            jira_ids=[],
            auto_fix=False,
        )
        assert "Add login feature" in prompt

    def test_prompt_contains_branch_info(self, sample_pr):
        """Test that prompt contains branch information."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff="",
            changed_files=[],
            jira_ids=[],
            auto_fix=False,
        )
        assert "feature/login" in prompt
        assert "main" in prompt

    def test_prompt_contains_author(self, sample_pr):
        """Test that prompt contains author information."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff="",
            changed_files=[],
            jira_ids=[],
            auto_fix=False,
        )
        assert "johndoe" in prompt

    def test_prompt_contains_jira_ids(self, sample_pr):
        """Test that prompt contains JIRA IDs when present."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff="",
            changed_files=[],
            jira_ids=["ABC-123", "DEF-456"],
            auto_fix=False,
        )
        assert "ABC-123" in prompt
        assert "DEF-456" in prompt

    def test_prompt_no_jira_ids(self, sample_pr):
        """Test prompt when no JIRA IDs are present."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff="",
            changed_files=[],
            jira_ids=[],
            auto_fix=False,
        )
        assert "No JIRA tickets found" in prompt

    def test_prompt_contains_diff(self, sample_pr, sample_diff):
        """Test that prompt contains the diff."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff=sample_diff,
            changed_files=[],
            jira_ids=[],
            auto_fix=False,
        )
        assert "def login():" in prompt

    def test_auto_fix_true(self, sample_pr):
        """Test that auto-fix mode includes edit permission instruction."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff="",
            changed_files=[],
            jira_ids=[],
            auto_fix=True,
        )
        assert "You have permission to make changes" in prompt

    def test_auto_fix_false(self, sample_pr):
        """Test that non-auto-fix mode includes plan-mode instruction."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff="",
            changed_files=[],
            jira_ids=[],
            auto_fix=False,
        )
        assert "Do NOT make any changes" in prompt

    def test_prompt_contains_changed_files(self, sample_pr, sample_files):
        """Test that prompt lists changed files."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff="",
            changed_files=sample_files,
            jira_ids=[],
            auto_fix=False,
        )
        assert "src/main.py" in prompt

    def test_prompt_json_format(self, sample_pr):
        """Test that prompt includes JSON output format."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff="",
            changed_files=[],
            jira_ids=[],
            auto_fix=False,
        )
        assert '"summary"' in prompt
        assert '"inline_comments"' in prompt
        assert '"verdict"' in prompt

    def test_prompt_no_pr_discussion_section(self, sample_pr):
        """Test that prompt does NOT contain PR Discussion section."""
        prompt = build_review_prompt(
            pr=sample_pr,
            diff="",
            changed_files=[],
            jira_ids=[],
            auto_fix=False,
        )
        assert "PR Discussion" not in prompt
