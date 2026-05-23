"""Tests for context gatherers module."""

import pytest
from unittest.mock import patch, MagicMock
from pr_review.agent_runner import AgentContext, JiraTicket, GitHubData
from pr_review.context_gatherers import (
    ContextGatherer,
    JiraContextGatherer,
    GitHubContextGatherer,
)


class TestJiraContextGatherer:
    """Tests for JiraContextGatherer."""

    def test_gather_no_jira_ids(self):
        """Test gather with no JIRA IDs in context."""
        gatherer = JiraContextGatherer()
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
        )
        # No jira_ids in metadata, should remain unchanged
        result = gatherer.gather(ctx)
        assert result.jira_tickets == []

    def test_gather_with_jira_ids_in_metadata(self):
        """Test gather with JIRA IDs in metadata."""
        gatherer = JiraContextGatherer()
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
            metadata={"jira_ids": ["PMAT-93", "PMAT-96"]},
        )

        # Mock the _fetch_jira_tickets to avoid actual API calls
        with patch.object(gatherer, '_fetch_jira_tickets', return_value=[
            JiraTicket(id="PMAT-93", summary="Test bug", status="In Review", priority="High"),
        ]):
            result = gatherer.gather(ctx)
            assert len(result.jira_tickets) == 1
            assert result.jira_tickets[0].id == "PMAT-93"

    def test_gather_preserves_existing_tickets(self):
        """Test that existing tickets are not overwritten."""
        gatherer = JiraContextGatherer()
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
            jira_tickets=[
                JiraTicket(id="PMAT-93", summary="Existing ticket", status="In Review", priority="High"),
            ],
        )
        result = gatherer.gather(ctx)
        assert len(result.jira_tickets) == 1
        assert result.jira_tickets[0].id == "PMAT-93"


class TestGitHubContextGatherer:
    """Tests for GitHubContextGatherer."""

    def test_gather_non_github_url(self):
        """Test gather with non-GitHub URL."""
        gatherer = GitHubContextGatherer()
        ctx = AgentContext(
            agent_name="test-agent",
            target="https://gitlab.com/org/repo/pull/123",
        )
        result = gatherer.gather(ctx)
        assert result.github_data is None

    def test_gather_github_pr_url(self):
        """Test gather with GitHub PR URL."""
        gatherer = GitHubContextGatherer()
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
        )

        mock_gh_data = GitHubData(
            pr_url="https://github.com/org/repo/pull/123",
            pr_title="Test PR",
            pr_body="A test PR",
            author="testuser",
            head_branch="feature/test",
            base_branch="main",
            state="open",
        )

        with patch.object(gatherer, '_fetch_pr_data', return_value=mock_gh_data):
            result = gatherer.gather(ctx)
            assert result.github_data is not None
            assert result.github_data.pr_title == "Test PR"

    def test_gather_preserves_existing_github_data(self):
        """Test that existing GitHub data is not overwritten."""
        gatherer = GitHubContextGatherer()
        existing_data = GitHubData(
            pr_url="https://github.com/org/repo/pull/123",
            pr_title="Existing PR",
            pr_body="",
            author="user",
            head_branch="old",
            base_branch="main",
            state="closed",
        )
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
            github_data=existing_data,
        )
        result = gatherer.gather(ctx)
        assert result.github_data == existing_data


class TestContextGatherer:
    """Tests for ContextGatherer abstract class."""

    def test_context_gatherer_is_abc(self):
        """Test that ContextGatherer cannot be instantiated directly."""
        with pytest.raises(TypeError):
            ContextGatherer()