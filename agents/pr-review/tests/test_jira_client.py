"""Unit tests for jira_client module."""

import pytest
from pr_review.jira_client import extract_jira_ids


class TestExtractJiraIds:
    """Tests for extract_jira_ids function."""

    def test_no_jira_ids(self):
        """Test with text that has no JIRA IDs."""
        result = extract_jira_ids("Fix login bug", "feature/login", "No body")
        assert result == []

    def test_single_id_in_title(self):
        """Test with a single JIRA ID in title."""
        result = extract_jira_ids("ABC-123 Fix login", "feature/abc-123", "")
        assert "ABC-123" in result

    def test_multiple_ids_in_title(self):
        """Test with multiple JIRA IDs in title."""
        result = extract_jira_ids("ABC-123 MYPROJ-456 Fix login", "feature/branch", "")
        assert "ABC-123" in result
        assert "MYPROJ-456" in result

    def test_id_in_branch_name(self):
        """Test extracting ID from branch name."""
        result = extract_jira_ids("Fix bug", "feature/ABC-123-my-feature", "")
        assert "ABC-123" in result

    def test_id_in_pr_body(self):
        """Test extracting ID from PR body."""
        result = extract_jira_ids("Fix bug", "feature/branch", "Related to ABC-123")
        assert "ABC-123" in result

    def test_duplicate_ids_deduplicated(self):
        """Test that duplicate IDs are deduplicated, preserving first appearance order."""
        result = extract_jira_ids("ABC-123 Fix", "feature/ABC-123", "See ABC-123")
        assert result.count("ABC-123") == 1
        assert result == ["ABC-123"]

    def test_multiple_unique_ids_order_preserved(self):
        """Test that order of first appearance is preserved."""
        result = extract_jira_ids("ABC-123 Fix", "feature/DEF-456", "Related XYZ-789")
        assert result == ["ABC-123", "DEF-456", "XYZ-789"]

    def test_lowercase_ignored(self):
        """Test that lowercase patterns are not matched (regex uses A-Z)."""
        result = extract_jira_ids("abc-123 Fix", "feature/branch", "")
        assert result == []

    def test_numeric_project_key(self):
        """Test that project keys with numbers are matched."""
        result = extract_jira_ids("A1B2-123 Fix", "feature/branch", "")
        assert "A1B2-123" in result

    def test_empty_inputs(self):
        """Test with empty strings."""
        result = extract_jira_ids("", "", "")
        assert result == []

    def test_none_body(self):
        """Test with None body (should be treated as empty string)."""
        result = extract_jira_ids("ABC-123", "branch", None)
        assert "ABC-123" in result
