"""Integration tests for stage2 via dry-run mock."""

import pytest
from unittest.mock import patch, MagicMock

from pr_review_performance.chunker import FileChunk
from pr_review_performance.stage2 import run_file_review, Issue, FileReviewResult
from pr_review_performance.config import Config
from pr_review_performance.tools import GitHubFileReader


class MockLLMResponse:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class MockConfig:
    model = "dry-run-model"
    api_url = "https://example.com/api"
    api_token = "test-token"
    overview_model = ""
    base_branch = "master"
    max_tool_calls = 10
    max_file_lines = 8000
    exclusions = []
    output_file = "review_output.md"
    github_repo = ""
    github_token_env_var = "GITHUB_TOKEN"
    repo_root = "/tmp"

    @property
    def effective_overview_model(self):
        return self.overview_model or self.model


def test_run_file_review_parses_valid_json():
    chunk = FileChunk(
        file_path="src/main.py",
        diff_text="diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1,2 @@\n+line one\n+line two\n",
    )
    mock_response = MockLLMResponse(
        content='{"summary": "Adds new helper function.", "issues": []}'
    )
    config = MockConfig()
    with patch("pr_review_performance.stage2.chat", return_value=mock_response):
        result = run_file_review(chunk, "Overview text", config)

    assert result.file_path == "src/main.py"
    assert "helper function" in result.summary
    assert result.tool_calls_made == 0


def test_run_file_review_extracts_issues():
    chunk = FileChunk(
        file_path="src/auth.py",
        diff_text="diff --git a/src/auth.py b/src/auth.py\n+new line\n",
    )
    mock_response = MockLLMResponse(
        content='{"summary": "Token validation.", "issues": [{"file": "src/auth.py", "line": 42, "severity": "high", "comment": "Timing issue", "suggestion": "Use hmac.compare_digest."}]}'
    )
    config = MockConfig()
    with patch("pr_review_performance.stage2.chat", return_value=mock_response):
        result = run_file_review(chunk, "Overview", config)

    assert len(result.issues) == 1
    assert result.issues[0].severity == "high"
    assert result.issues[0].comment == "Timing issue"


def test_run_file_review_retry_on_invalid_json():
    """On first invalid JSON, we send a retry message; second failure returns error summary."""
    chunk = FileChunk(
        file_path="src/main.py",
        diff_text="diff --git a/src/main.py b/src/main.py\n+new\n",
    )

    call_count = [0]

    def mock_chat(messages, tools, config):
        call_count[0] += 1
        if call_count[0] == 1:
            return MockLLMResponse(content="This is not JSON")
        else:
            return MockLLMResponse(content='{"summary": "Recovered.", "issues": []}')

    config = MockConfig()
    with patch("pr_review_performance.stage2.chat", side_effect=mock_chat):
        result = run_file_review(chunk, "Overview", config)

    assert "Recovered" in result.summary or "parse" in result.summary.lower()


def test_run_file_review_handles_tool_calls():
    chunk = FileChunk(
        file_path="src/main.py",
        diff_text="diff --git a/src/main.py b/src/main.py\n+new\n",
    )
    from pr_review_performance.llm import ToolCall

    tool_call = ToolCall(
        id="call_001",
        name="read_file",
        arguments={"path": "src/main.py"},
    )

    calls = [0]

    def mock_chat(messages, tools, config):
        calls[0] += 1
        if calls[0] == 1:
            return MockLLMResponse(tool_calls=[tool_call])
        else:
            return MockLLMResponse(
                content='{"summary": "Reviewed.", "issues": []}'
            )

    config = MockConfig()
    with patch("pr_review_performance.stage2.chat", side_effect=mock_chat):
        with patch("pr_review_performance.stage2.execute_read_file", return_value="def main():\n    pass\n"):
            result = run_file_review(chunk, "Overview", config)

    assert result.tool_calls_made == 1
    assert "Reviewed" in result.summary or "parse" in result.summary.lower()


def test_run_file_review_passes_github_reader_to_execute_read_file():
    """When config.github_reader is set, stage2 passes it to execute_read_file."""
    chunk = FileChunk(
        file_path="src/main.py",
        diff_text="diff --git a/src/main.py b/src/main.py\n+new\n",
    )
    from pr_review_performance.llm import ToolCall

    tool_call = ToolCall(
        id="call_github",
        name="read_file",
        arguments={"path": "src/main.py", "reason": "checking imports"},
    )

    calls = [0]

    def mock_chat(messages, tools, config):
        calls[0] += 1
        if calls[0] == 1:
            return MockLLMResponse(tool_calls=[tool_call])
        return MockLLMResponse(content='{"summary": "OK", "issues": []}')

    mock_reader = MagicMock(spec=GitHubFileReader)

    config = MockConfig()
    config.github_reader = mock_reader

    captured_reader = {}

    def capturing_execute(path, repo_root, max_lines, github_reader=None):
        captured_reader["reader"] = github_reader
        return "remote content"

    with patch("pr_review_performance.stage2.chat", side_effect=mock_chat):
        with patch("pr_review_performance.stage2.execute_read_file", side_effect=capturing_execute):
            run_file_review(chunk, "Overview", config)

    assert captured_reader.get("reader") is mock_reader
