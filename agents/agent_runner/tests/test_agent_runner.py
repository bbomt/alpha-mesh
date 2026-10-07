"""Tests for the agent_runner framework (moved from agents/pr-review/tests, PMAT-229)."""

import subprocess
import pytest
from agent_runner import (
    AgentContext,
    JiraTicket,
    GitHubData,
    PluginHandler,
    ClaudeInvocationError,
    run_claude_headless,
)


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


class TestRunClaudeHeadless:
    """Tests for run_claude_headless function."""

    def test_returns_output_on_success(self, mocker):
        """Test that run_claude_headless returns stdout on successful exit."""
        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Claude completed successfully"
        mock_result.stderr = ""

        mocker.patch("subprocess.run", return_value=mock_result)
        mocker.patch("os.unlink")

        result = run_claude_headless("test prompt", "/fake/repo")
        assert result == "Claude completed successfully"

    def test_raises_on_nonzero_exit(self, mocker):
        """Test that run_claude_headless raises ClaudeInvocationError on failure."""
        mock_result = mocker.MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Some error occurred"

        mocker.patch("subprocess.run", return_value=mock_result)
        mocker.patch("os.unlink")

        with pytest.raises(ClaudeInvocationError) as exc_info:
            run_claude_headless("test prompt", "/fake/repo")
        assert "Claude exited with code 1" in str(exc_info.value)
        assert "Some error occurred" in str(exc_info.value)

    def test_raises_on_timeout(self, mocker):
        """Test that run_claude_headless raises ClaudeInvocationError on timeout."""
        mocker.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["claude"], 300))
        mocker.patch("os.unlink")

        with pytest.raises(ClaudeInvocationError) as exc_info:
            run_claude_headless("test prompt", "/fake/repo", timeout=300)
        assert "timed out after 300s" in str(exc_info.value)

    def test_raises_on_missing_binary(self, mocker):
        """Test that run_claude_headless raises ClaudeInvocationError when binary not found."""
        mocker.patch("subprocess.run", side_effect=FileNotFoundError)
        mocker.patch("os.unlink")

        with pytest.raises(ClaudeInvocationError) as exc_info:
            run_claude_headless("test prompt", "/fake/repo")
        assert "Claude CLI not found" in str(exc_info.value)

    def test_raises_on_timeout_with_verbose(self, mocker):
        """Test that run_claude_headless raises on timeout and logs to stderr in verbose mode."""
        mocker.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["claude"], 600))
        mocker.patch("os.unlink")

        with pytest.raises(ClaudeInvocationError) as exc_info:
            run_claude_headless("test prompt", "/fake/repo", timeout=600, verbose=True)
        assert "timed out after 600s" in str(exc_info.value)

    def test_success_with_verbose_prints_debug_info(self, mocker, capsys):
        """Test that run_claude_headless with verbose=True prints debug info to stderr."""
        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "output"
        mock_result.stderr = ""

        mocker.patch("subprocess.run", return_value=mock_result)
        mocker.patch("os.unlink")

        run_claude_headless("test prompt", "/fake/repo", verbose=True)

        _, stderr = capsys.readouterr()
        assert "/fake/repo" in stderr
        assert "Prompt length" in stderr
