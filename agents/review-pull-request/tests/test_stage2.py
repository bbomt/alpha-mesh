"""Integration tests for stage2 via dry-run mock."""

import pytest
from unittest.mock import patch, MagicMock

from review_pull_request.chunker import FileChunk
from review_pull_request.stage2 import run_file_review, Issue, FileReviewResult
from review_pull_request.config import Config
from review_pull_request.tools import GitHubFileReader, ReadError


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
    with patch("review_pull_request.stage2.chat", return_value=mock_response):
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
    with patch("review_pull_request.stage2.chat", return_value=mock_response):
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
    with patch("review_pull_request.stage2.chat", side_effect=mock_chat):
        result = run_file_review(chunk, "Overview", config)

    assert result.summary == "Recovered."
    assert call_count[0] == 2


def test_run_file_review_handles_tool_calls():
    chunk = FileChunk(
        file_path="src/main.py",
        diff_text="diff --git a/src/main.py b/src/main.py\n+new\n",
    )
    from review_pull_request.llm import ToolCall

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
    with patch("review_pull_request.stage2.chat", side_effect=mock_chat):
        with patch("review_pull_request.stage2.execute_read_file", return_value="def main():\n    pass\n"):
            result = run_file_review(chunk, "Overview", config)

    assert result.tool_calls_made == 1
    assert result.summary == "Reviewed."


def test_run_file_review_passes_github_reader_to_execute_read_file():
    """When config.github_reader is set, stage2 passes it to execute_read_file."""
    chunk = FileChunk(
        file_path="src/main.py",
        diff_text="diff --git a/src/main.py b/src/main.py\n+new\n",
    )
    from review_pull_request.llm import ToolCall

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

    with patch("review_pull_request.stage2.chat", side_effect=mock_chat):
        with patch("review_pull_request.stage2.execute_read_file", side_effect=capturing_execute):
            run_file_review(chunk, "Overview", config)

    assert captured_reader.get("reader") is mock_reader


def test_run_file_review_raises_on_llm_error():
    """A failed LLM call must propagate, not return an empty 'LLM call failed.' result."""
    from review_pull_request.llm import LLMError

    chunk = FileChunk(file_path="src/main.py", diff_text="diff --git a/src/main.py b/src/main.py\n+x\n")
    with patch("review_pull_request.stage2.chat", side_effect=LLMError("API returned 404")):
        with pytest.raises(LLMError):
            run_file_review(chunk, "Overview text", MockConfig())


# --- read_file caching (PMAT-229) ---

def _tool_call(call_id, path):
    from types import SimpleNamespace
    return SimpleNamespace(id=call_id, name="read_file", arguments={"path": path, "reason": "check"})


def _scripted_chat(*tool_call_rounds):
    """chat() stub: one tool-call round per entry, then a final JSON answer."""
    rounds = list(tool_call_rounds)

    def fake(messages, tools, config):
        if rounds:
            return MockLLMResponse(tool_calls=rounds.pop(0))
        return MockLLMResponse(content='{"summary": "ok", "issues": []}')

    return fake


def _chunk(path="src/a.py"):
    return FileChunk(file_path=path, diff_text=f"diff --git a/{path} b/{path}\n+x\n")


def test_repeated_read_in_one_review_hits_the_file_once_and_tells_the_model():
    scripted = _scripted_chat([_tool_call("1", "pyproject.toml")], [_tool_call("2", "pyproject.toml")])
    seen_messages = []

    def capturing(messages, tools, config):
        seen_messages.append(list(messages))
        return scripted(messages, tools, config)

    with patch("review_pull_request.stage2.chat", side_effect=capturing), \
         patch("review_pull_request.stage2.execute_read_file", return_value="FILE BODY") as ex:
        result = run_file_review(_chunk(), "overview", MockConfig())

    assert ex.call_count == 1
    tool_msgs = [m["content"] for m in seen_messages[-1] if m.get("role") == "tool"]
    assert tool_msgs[0] == "FILE BODY"
    assert "already" in tool_msgs[1].lower()
    assert result.summary == "ok"


def test_read_cache_is_shared_across_file_reviews_in_one_run():
    config = MockConfig()
    config.read_cache = {}
    with patch("review_pull_request.stage2.chat", side_effect=_scripted_chat([_tool_call("1", "pyproject.toml")])), \
         patch("review_pull_request.stage2.execute_read_file", return_value="FILE BODY") as first:
        run_file_review(_chunk("a.py"), "o", config)
    with patch("review_pull_request.stage2.chat", side_effect=_scripted_chat([_tool_call("2", "pyproject.toml")])), \
         patch("review_pull_request.stage2.execute_read_file", return_value="FILE BODY") as second:
        run_file_review(_chunk("b.py"), "o", config)

    assert first.call_count == 1
    assert second.call_count == 0  # served from the shared cache
    assert config.read_cache == {"pyproject.toml": "FILE BODY"}


def test_error_results_are_not_cached():
    config = MockConfig()
    config.read_cache = {}
    with patch("review_pull_request.stage2.chat", side_effect=_scripted_chat([_tool_call("1", "missing.py")])), \
         patch("review_pull_request.stage2.execute_read_file", return_value=ReadError("Error: file not found: missing.py")):
        run_file_review(_chunk(), "o", config)

    assert config.read_cache == {}


def test_file_whose_content_starts_with_error_is_still_cached():
    config = MockConfig()
    config.read_cache = {}
    with patch("review_pull_request.stage2.chat", side_effect=_scripted_chat([_tool_call("1", "TROUBLESHOOTING.md")])), \
         patch("review_pull_request.stage2.execute_read_file", return_value="Error: connection refused\nfix: restart"):
        run_file_review(_chunk(), "o", config)

    assert config.read_cache == {"TROUBLESHOOTING.md": "Error: connection refused\nfix: restart"}


def test_read_cache_stops_storing_once_the_size_cap_is_reached():
    config = MockConfig()
    config.read_cache = {}
    config.read_cache_max_bytes = 10
    chat_stub = _scripted_chat([_tool_call("1", "a.py")], [_tool_call("2", "b.py")])
    bodies = {"a.py": "x" * 8, "b.py": "y" * 8}  # second would take the cache to 16 > 10
    seen_tool_messages = []

    def capturing(messages, tools, config):
        seen_tool_messages[:] = [m["content"] for m in messages if m.get("role") == "tool"]
        return chat_stub(messages, tools, config)

    with patch("review_pull_request.stage2.chat", side_effect=capturing), \
         patch("review_pull_request.stage2.execute_read_file", side_effect=lambda p, *a: bodies[p]):
        run_file_review(_chunk(), "o", config)

    assert seen_tool_messages == ["x" * 8, "y" * 8]  # both still served to the model
    assert config.read_cache == {"a.py": "x" * 8}    # but only the first was stored


def test_read_file_log_line_names_the_file_under_review(capsys):
    with patch("review_pull_request.stage2.chat", side_effect=_scripted_chat([_tool_call("1", "pyproject.toml")])), \
         patch("review_pull_request.stage2.execute_read_file", return_value="FILE BODY"):
        run_file_review(_chunk("src/a.py"), "o", MockConfig())

    err = capsys.readouterr().err
    assert "read_file: pyproject.toml" in err
    assert "reviewing src/a.py" in err.split("read_file: pyproject.toml")[1].splitlines()[0]


def test_failed_read_can_be_retried_within_the_same_review():
    scripted = _scripted_chat([_tool_call("1", "pyproject.toml")], [_tool_call("2", "pyproject.toml")])
    seen = []

    def capturing(messages, tools, config):
        seen.append([m["content"] for m in messages if m.get("role") == "tool"])
        return scripted(messages, tools, config)

    outcomes = iter([ReadError("Error: GitHub API rate limit exceeded."), "FILE BODY"])
    with patch("review_pull_request.stage2.chat", side_effect=capturing), \
         patch("review_pull_request.stage2.execute_read_file", side_effect=lambda *a: next(outcomes)) as ex:
        run_file_review(_chunk(), "o", MockConfig())

    assert ex.call_count == 2  # the failed read was not treated as "already read"
    assert seen[-1] == ["Error: GitHub API rate limit exceeded.", "FILE BODY"]


def test_read_cache_bytes_counter_tracks_stored_content():
    config = MockConfig()
    config.read_cache = {}
    config.read_cache_bytes = 0
    with patch("review_pull_request.stage2.chat", side_effect=_scripted_chat([_tool_call("1", "a.py"), _tool_call("2", "b.py")])), \
         patch("review_pull_request.stage2.execute_read_file", side_effect=lambda p, *a: {"a.py": "xxx", "b.py": "yy\u00e9"}[p]):
        run_file_review(_chunk(), "o", config)

    assert config.read_cache_bytes == len("xxx".encode()) + len("yy\u00e9".encode())


# --- severity normalisation, incomplete results, JSON extraction ---

def _final(issues_json):
    return MockLLMResponse(content='{"summary": "s", "issues": %s}' % issues_json)


def test_unknown_severities_are_normalised_so_findings_are_not_dropped():
    issues = (
        '[{"file":"a","line":1,"severity":"error","comment":"c1"},'
        '{"file":"a","line":2,"severity":"critical","comment":"c2"},'
        '{"file":"a","line":3,"severity":"warning","comment":"c3"},'
        '{"file":"a","line":4,"severity":"weird","comment":"c4"},'
        '{"file":"a","line":5,"severity":"HIGH","comment":"c5"}]'
    )
    with patch("review_pull_request.stage2.chat", return_value=_final(issues)):
        result = run_file_review(_chunk(), "o", MockConfig())

    assert [i.severity for i in result.issues] == ["high", "high", "medium", "low", "high"]


def test_unparseable_final_response_marks_the_result_incomplete():
    with patch("review_pull_request.stage2.chat", return_value=MockLLMResponse(content="not json at all")):
        result = run_file_review(_chunk(), "o", MockConfig())

    assert result.incomplete is True


def test_max_tool_calls_marks_the_result_incomplete():
    config = MockConfig()
    config.max_tool_calls = 1
    with patch("review_pull_request.stage2.chat", side_effect=_scripted_chat([_tool_call("1", "a.py")])), \
         patch("review_pull_request.stage2.execute_read_file", return_value="x"):
        result = run_file_review(_chunk(), "o", config)

    assert result.incomplete is True


def test_a_normal_result_is_not_incomplete():
    with patch("review_pull_request.stage2.chat", return_value=_final("[]")):
        result = run_file_review(_chunk(), "o", MockConfig())

    assert result.incomplete is False


def test_json_is_found_inside_prose_and_fences():
    from review_pull_request.stage2 import _parse_json_response

    body = '{"summary": "ok", "issues": []}'
    assert _parse_json_response("Here is my review:\n```json\n" + body + "\n```\nHope it helps.") == {"summary": "ok", "issues": []}
    assert _parse_json_response("Sure! " + body + " Let me know.") == {"summary": "ok", "issues": []}
    assert _parse_json_response("```json\n" + body + "\n```") == {"summary": "ok", "issues": []}
    assert _parse_json_response("nothing here") is None
