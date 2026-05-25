"""Tests for agent runner module."""

import pytest
from pr_review.agent_runner import (
    AgentContext,
    JiraTicket,
    GitHubData,
    PluginHandler,
    ClaudeInvocationError,
)
from pr_review.plugin_handler import CodeReviewPluginHandler


class TestAgentContext:
    """Tests for AgentContext dataclass."""

    def test_to_text_empty(self):
        """Test to_text with minimal context."""
        ctx = AgentContext(
            agent_name="test-agent",
            target="https://github.com/org/repo/pull/123",
        )
        text = ctx.to_text()
        assert "Agent: test-agent" in text
        assert "Target: https://github.com/org/repo/pull/123" in text

    def test_to_text_with_jira(self):
        """Test to_text with JIRA tickets."""
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
            jira_tickets=[
                JiraTicket(id="PMAT-93", summary="Profile form bugs", status="In Review", priority="High"),
                JiraTicket(id="PMAT-96", summary="Handle validation", status="Done", priority="Medium"),
            ],
        )
        text = ctx.to_text()
        assert "PMAT-93" in text
        assert "PMAT-96" in text
        assert "Profile form bugs" in text
        assert "Profile form bugs" in text

    def test_to_text_with_github(self):
        """Test to_text with GitHub data."""
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
            github_data=GitHubData(
                pr_url="https://github.com/org/repo/pull/123",
                pr_title="fix: profile form e2e tests",
                pr_body="Fixes PMAT-93",
                author="johndoe",
                head_branch="PMAT-93-profile-form",
                base_branch="main",
                state="open",
                changed_files=["packages/fe/src/ProfileForm.tsx"],
            ),
        )
        text = ctx.to_text()
        assert "fix: profile form e2e tests" in text
        assert "johndoe" in text
        assert "PMAT-93-profile-form" in text
        assert "main" in text

    def test_to_text_with_metadata(self):
        """Test to_text with metadata."""
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
            metadata={"repo": "PMATFE-mfe-profile", "pr_number": "123"},
        )
        text = ctx.to_text()
        assert "repo=PMATFE-mfe-profile" in text
        assert "pr_number=123" in text


class TestCodeReviewPluginHandler:
    """Tests for CodeReviewPluginHandler."""

    def test_plugin_name(self):
        """Test plugin name property."""
        handler = CodeReviewPluginHandler()
        assert handler.plugin_name == "code-review"

    def test_build_prompt_minimal(self):
        """Test prompt building with minimal context."""
        handler = CodeReviewPluginHandler()
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
        )
        prompt = handler.build_prompt(ctx)
        assert "/code-review medium" in prompt

    def test_build_prompt_with_jira(self):
        """Test prompt building with JIRA tickets."""
        handler = CodeReviewPluginHandler()
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
            jira_tickets=[
                JiraTicket(id="PMAT-93", summary="Profile form bugs", status="In Review", priority="High"),
            ],
        )
        prompt = handler.build_prompt(ctx)
        assert "/code-review medium" in prompt
        assert "PMAT-93" in prompt
        assert "Profile form bugs" in prompt

    def test_build_prompt_with_github(self):
        """Test prompt building with GitHub data."""
        handler = CodeReviewPluginHandler()
        ctx = AgentContext(
            agent_name="pr-review",
            target="https://github.com/org/repo/pull/123",
            github_data=GitHubData(
                pr_url="https://github.com/org/repo/pull/123",
                pr_title="fix: profile form e2e tests",
                pr_body="Fixes PMAT-93",
                author="johndoe",
                head_branch="PMAT-93-profile-form",
                base_branch="main",
                state="open",
            ),
        )
        prompt = handler.build_prompt(ctx)
        assert "fix: profile form e2e tests" in prompt
        assert "johndoe" in prompt
        assert "PMAT-93-profile-form" in prompt

    def test_parse_output_no_issues(self):
        """Test parsing output with no issues found."""
        handler = CodeReviewPluginHandler()
        ctx = AgentContext(agent_name="pr-review", target="https://github.com/org/repo/pull/123")
        output = """### Code review

No issues found. Checked for bugs and CLAUDE.md compliance.

🤖 Generated with [Claude Code](https://claude.ai/code)"""
        result = handler.parse_output(output, ctx)
        assert result["issues_found"] is False
        assert result["issue_count"] == 0
        assert "No issues found" in result["review_text"]

    def test_parse_output_with_issues(self):
        """Test parsing output with issues found."""
        handler = CodeReviewPluginHandler()
        ctx = AgentContext(agent_name="pr-review", target="https://github.com/org/repo/pull/123")
        output = """### Code review

Found 3 issues:

1. Bug description (CLAUDE.md says "...")
https://github.com/org/repo/blob/sha/file.ts#L10-L15

2. Another issue
https://github.com/org/repo/blob/sha/file.ts#L20-L25

🤖 Generated with [Claude Code](https://claude.ai/code)"""
        result = handler.parse_output(output, ctx)
        assert result["issues_found"] is True
        assert result["issue_count"] == 3

    def test_parse_output_unknown_format(self):
        """Test parsing output with unknown format."""
        handler = CodeReviewPluginHandler()
        ctx = AgentContext(agent_name="pr-review", target="https://github.com/org/repo/pull/123")
        output = "Some arbitrary output that doesn't match expected format"
        result = handler.parse_output(output, ctx)
        assert result["issues_found"] is False
        assert result["issue_count"] == 0


class TestPluginHandler:
    """Tests for PluginHandler abstract class."""

    def test_plugin_handler_is_abc(self):
        """Test that PluginHandler cannot be instantiated directly."""
        with pytest.raises(TypeError):
            PluginHandler()


class TestClaudeInvocationError:
    """Tests for ClaudeInvocationError exception."""

    def test_exception_message(self):
        """Test exception carries message."""
        err = ClaudeInvocationError("Claude timed out")
        assert str(err) == "Claude timed out"

    def test_exception_inheritance(self):
        """Test exception inherits from Exception."""
        err = ClaudeInvocationError("test")
        assert isinstance(err, Exception)