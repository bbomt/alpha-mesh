"""
Tests for dream module.
Tests the Python implementation of the dream feature.
"""

import json
import os
import tempfile
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

# Import the module under test
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dream import (
    extract_sessions_from_jsonl,
    extract_response_text,
    extract_learnings,
    synthesize_memory,
    create_new_memory,
    MAX_MEMORY_LINES,
    split_routing,
    enforce_recent_signals_cap,
    check_conflicts,
    is_real_user_message,
)


class TestExtractResponseText:
    """Tests for extract_response_text function."""

    def test_extracts_anthropic_text_block(self):
        """Test extraction from Anthropic response format."""
        response = json.dumps({
            "content": [
                {"type": "text", "text": "Hello world"}
            ]
        })
        assert extract_response_text(response) == "Hello world"

    def test_extracts_openai_choice(self):
        """Test extraction from OpenAI response format."""
        response = json.dumps({
            "choices": [
                {"message": {"content": "OpenAI response"}}
            ]
        })
        assert extract_response_text(response) == "OpenAI response"

    def test_returns_plain_text_when_not_json(self):
        """Test that plain text is returned as-is."""
        assert extract_response_text("Just plain text") == "Just plain text"

    def test_returns_empty_for_empty_string(self):
        """Test empty string handling."""
        assert extract_response_text("") == ""

    def test_returns_empty_for_none(self):
        """Test None handling."""
        assert extract_response_text(None) == ""

    def test_extracts_openrouter_response_field(self):
        """Test extraction from OpenRouter response field."""
        response = json.dumps({
            "response": "### Architecture\n- JWT auth pattern"
        })
        assert extract_response_text(response) == "### Architecture\n- JWT auth pattern"

    def test_extracts_openrouter_text_field(self):
        """Test extraction from OpenRouter text field."""
        response = json.dumps({
            "text": "### Patterns\n- Repository pattern"
        })
        assert extract_response_text(response) == "### Patterns\n- Repository pattern"

    def test_extracts_openrouter_answer_field(self):
        """Test extraction from OpenRouter answer field."""
        response = json.dumps({
            "answer": "### Workflow\n- Test first"
        })
        assert extract_response_text(response) == "### Workflow\n- Test first"

    def test_extracts_openrouter_nested_learnings(self):
        """Test extraction from OpenRouter nested learnings format."""
        response = json.dumps({
            "learnings": {
                "Architecture": ["Modular monolith architecture"],
                "Patterns": ["Repository pattern for data access"]
            }
        })
        result = extract_response_text(response)
        assert "### Architecture" in result
        assert "Modular monolith" in result
        assert "### Patterns" in result
        assert "Repository pattern" in result

    def test_extracts_openrouter_validation_wrapper_skipped(self):
        """Test that OpenRouter validation wrapper {is_valid: true} returns empty."""
        response = json.dumps({
            "is_valid": True,
            "review_reason": "format",
            "reviewer_comment": "looks good"
        })
        assert extract_response_text(response) == ""

    def test_extracts_section_fields_with_arrays(self):
        """Test extraction from section fields with list values."""
        response = json.dumps({
            "architecture": ["Modular architecture", "Clean domain boundaries"],
            "patterns": ["Repository pattern"],
            "workflow": ["Test-driven development"]
        })
        result = extract_response_text(response)
        assert "### Architecture" in result
        assert "Modular architecture" in result
        assert "Clean domain boundaries" in result
        assert "### Patterns" in result
        assert "Repository pattern" in result


class TestCreateNewMemory:
    """Tests for create_new_memory function."""

    def test_creates_memory_with_correct_format(self):
        """Test that new memory has correct header and content."""
        result = create_new_memory(
            new_learnings="Some useful learning",
            date="2026-05-11",
            repo_name="test-repo"
        )

        assert "# Memory: test-repo" in result
        assert "2026-05-11" in result
        assert "Some useful learning" in result

    def test_memory_has_required_sections(self):
        """Test memory structure."""
        result = create_new_memory(
            new_learnings="Content here",
            date="2026-05-11",
            repo_name="my-repo"
        )

        # Should have markdown header
        assert result.startswith("# Memory: my-repo")
        # Should have date section
        assert "## 2026-05-11" in result


class TestExtractSessionsFromJsonl:
    """Tests for extract_sessions_from_jsonl function."""

    def test_extracts_session_entries_correctly(self, tmp_path):
        """Test that session entries are parsed with correct structure."""
        # Create a temporary projects directory structure
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Create a mock session file
        session_file = projects_dir / "test-session" / "session.jsonl"
        session_file.parent.mkdir()
        session_file.write_text(json.dumps({
            "cwd": "/home/user/repos/my-project",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "This is a very long assistant message that is over 100 characters and should be extracted properly by the dream processing logic."}
                ]
            }
        }) + "\n")
        with session_file.open(mode="a") as f:
            f.write(json.dumps({
                "cwd": "/home/user/repos/my-project",
                "message": {
                    "role": "user",
                    "content": "This is a user message that is over 20 characters"
                }
            }) + "\n")

        # Set file mtime to today
        import time
        now = time.time()
        os.utime(session_file, (now, now))

        cutoff = datetime.now().strftime("%Y-%m-%d")

        result = extract_sessions_from_jsonl(
            projects_dir=str(projects_dir),
            cutoff=cutoff,
            target_repo=None
        )

        # Verify structure of returned data
        assert len(result) == 1
        repo_path = list(result.keys())[0]
        assert "my-project" in repo_path
        assert "content" in result[repo_path]
        assert "dates" in result[repo_path]
        assert "file_count" in result[repo_path]
        assert result[repo_path]["file_count"] >= 1

    def test_filters_by_target_repo(self, tmp_path):
        """Test that target_repo filtering works."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Create two session files in different repos
        repo1_session = projects_dir / "repo1-session" / "session.jsonl"
        repo1_session.parent.mkdir()
        with repo1_session.open(mode="w") as f:
            f.write(json.dumps({
                "cwd": "/home/user/repos/repo1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "A" * 150}]}
            }) + "\n")
            f.write(json.dumps({
                "cwd": "/home/user/repos/repo1",
                "message": {"role": "user", "content": "User message for repo1"}
            }) + "\n")

        repo2_session = projects_dir / "repo2-session" / "session.jsonl"
        repo2_session.parent.mkdir()
        with repo2_session.open(mode="w") as f:
            f.write(json.dumps({
                "cwd": "/home/user/repos/repo2",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "B" * 150}]}
            }) + "\n")
            f.write(json.dumps({
                "cwd": "/home/user/repos/repo2",
                "message": {"role": "user", "content": "User message for repo2"}
            }) + "\n")

        # Set mtime to today
        import time
        now = time.time()
        os.utime(repo1_session, (now, now))
        os.utime(repo2_session, (now, now))

        cutoff = datetime.now().strftime("%Y-%m-%d")

        # Filter to only repo1
        result = extract_sessions_from_jsonl(
            projects_dir=str(projects_dir),
            cutoff=cutoff,
            target_repo="/home/user/repos/repo1"
        )

        assert len(result) == 1
        assert "repo1" in list(result.keys())[0]

    def test_skips_short_messages(self, tmp_path):
        """Test that messages below length thresholds are skipped."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        session_file = projects_dir / "test-session" / "session.jsonl"
        session_file.parent.mkdir()

        # Write a short assistant message (< 100 chars) and short user message (< 20 chars)
        session_file.write_text(json.dumps({
            "cwd": "/home/user/repos/test",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Short"}]}
        }) + "\n")
        session_file.write_text(json.dumps({
            "cwd": "/home/user/repos/test",
            "message": {"role": "user", "content": "Short"}
        }) + "\n")

        import time
        now = time.time()
        os.utime(session_file, (now, now))

        cutoff = datetime.now().strftime("%Y-%m-%d")

        result = extract_sessions_from_jsonl(
            projects_dir=str(projects_dir),
            cutoff=cutoff,
            target_repo=None
        )

        # Should have no content since messages were too short
        assert len(result) == 0

    def test_handles_non_jsonl_lines(self, tmp_path):
        """Test that invalid JSON lines are skipped."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        session_file = projects_dir / "test-session" / "session.jsonl"
        session_file.parent.mkdir()

        # Write valid (user message) and invalid lines
        with session_file.open(mode="w") as f:
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {"role": "user", "content": "Valid user message for testing"}
            }) + "\n")
            f.write("this is not json\n")

        import time
        now = time.time()
        os.utime(session_file, (now, now))

        cutoff = datetime.now().strftime("%Y-%m-%d")

        result = extract_sessions_from_jsonl(
            projects_dir=str(projects_dir),
            cutoff=cutoff,
            target_repo=None
        )

        # Should still extract the valid entry
        assert len(result) == 1

    def test_default_excludes_assistant_messages(self, tmp_path):
        """Test that assistant messages are excluded by default (include_assistant=False)."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        session_file = projects_dir / "test-session" / "session.jsonl"
        session_file.parent.mkdir()

        # Write assistant message (long enough) and user message
        with session_file.open(mode="w") as f:
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "A" * 150}]
                }
            }) + "\n")
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "user",
                    "content": "This is a user message that is long enough"
                }
            }) + "\n")

        import time
        now = time.time()
        os.utime(session_file, (now, now))

        cutoff = datetime.now().strftime("%Y-%m-%d")

        result = extract_sessions_from_jsonl(
            projects_dir=str(projects_dir),
            cutoff=cutoff,
            target_repo=None,
            include_assistant=False  # default
        )

        # Should have one repo
        assert len(result) == 1
        content = result[list(result.keys())[0]]["content"]
        # Assistant message should NOT be present
        assert "[assistant]" not in content
        # User message should be present
        assert "[user]" in content

    def test_include_assistant_flag_includes_both(self, tmp_path):
        """Test that include_assistant=True includes both user and assistant messages."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        session_file = projects_dir / "test-session" / "session.jsonl"
        session_file.parent.mkdir()

        # Write assistant message and user message
        with session_file.open(mode="w") as f:
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "A" * 150}]
                }
            }) + "\n")
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "user",
                    "content": "This is a user message that is long enough"
                }
            }) + "\n")

        import time
        now = time.time()
        os.utime(session_file, (now, now))

        cutoff = datetime.now().strftime("%Y-%m-%d")

        result = extract_sessions_from_jsonl(
            projects_dir=str(projects_dir),
            cutoff=cutoff,
            target_repo=None,
            include_assistant=True
        )

        # Should have one repo
        assert len(result) == 1
        content = result[list(result.keys())[0]]["content"]
        # Both messages should be present
        assert "[assistant]" in content
        assert "[user]" in content
    def test_filters_out_claude_code_generated_messages(self, tmp_path):
        """Test that Claude Code generated messages (with internal tags) are filtered out."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        session_file = projects_dir / "test-session" / "session.jsonl"
        session_file.parent.mkdir()

        # Write messages - some are Claude Code generated (with tags), some are real user messages
        with session_file.open(mode="w") as f:
            # This is Claude Code generated - should be filtered out
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "user",
                    "content": "<command-name>/exit</command-name>"
                }
            }) + "\n")
            # This is also Claude Code generated - should be filtered out
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "user",
                    "content": "<system-reminder>Some reminder</system-reminder>"
                }
            }) + "\n")
            # This is a real user message - should be kept
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "user",
                    "content": "This is a real user message for testing"
                }
            }) + "\n")

        import time
        now = time.time()
        os.utime(session_file, (now, now))

        cutoff = datetime.now().strftime("%Y-%m-%d")

        result = extract_sessions_from_jsonl(
            projects_dir=str(projects_dir),
            cutoff=cutoff,
            target_repo=None,
            include_assistant=False
        )

        # Should have one repo
        assert len(result) == 1
        content = result[list(result.keys())[0]]["content"]
        # Real user message should be present
        assert "This is a real user message" in content
        # Claude Code generated messages should NOT be present
        assert "<command-name>" not in content
        assert "<system-reminder>" not in content

    def test_filters_task_notification_messages(self, tmp_path):
        """Test that <task-notification> harness messages are filtered out."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        session_file = projects_dir / "test-session" / "session.jsonl"
        session_file.parent.mkdir()

        with session_file.open(mode="w") as f:
            # task-notification harness message - should be filtered
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "user",
                    "content": (
                        "<task-notification>\n"
                        "<task-id>bawb2702u</task-id>\n"
                        "<status>killed</status>\n"
                        "</task-notification>"
                    )
                }
            }) + "\n")
            # Real user message - should be kept
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "user",
                    "content": "This is a real user message that should be kept"
                }
            }) + "\n")

        import time
        os.utime(session_file, (time.time(), time.time()))
        cutoff = datetime.now().strftime("%Y-%m-%d")

        result = extract_sessions_from_jsonl(
            projects_dir=str(projects_dir),
            cutoff=cutoff,
            target_repo=None,
        )

        assert len(result) == 1
        content = result[list(result.keys())[0]]["content"]
        assert "real user message that should be kept" in content
        assert "task-notification" not in content

    def test_extracts_command_args_from_skill_invocation(self, tmp_path):
        """Test that user intent is extracted from <command-args> when a skill is invoked."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        session_file = projects_dir / "test-session" / "session.jsonl"
        session_file.parent.mkdir()

        with session_file.open(mode="w") as f:
            # Skill invocation: harness wrapper containing user's actual args
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "user",
                    "content": (
                        "<command-message>feature-dev:feature-dev</command-message>\n"
                        "<command-name>/feature-dev:feature-dev</command-name>\n"
                        "<command-args>We need to implement the new PMAT-999 feature for filtering dream output</command-args>\n"
                        "\n# Feature Development\n\nYou are helping a developer implement a new feature..."
                    )
                }
            }) + "\n")

        import time
        os.utime(session_file, (time.time(), time.time()))
        cutoff = datetime.now().strftime("%Y-%m-%d")

        result = extract_sessions_from_jsonl(
            projects_dir=str(projects_dir),
            cutoff=cutoff,
            target_repo=None,
        )

        assert len(result) == 1
        content = result[list(result.keys())[0]]["content"]
        # User's original intent should be preserved
        assert "PMAT-999 feature for filtering dream output" in content
        # Harness wrapper tags should NOT appear
        assert "<command-message>" not in content
        assert "<command-name>" not in content

    def test_filters_skill_template_list_content(self, tmp_path):
        """Test that skill template messages (list-content starting with # heading) are filtered."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        session_file = projects_dir / "test-session" / "session.jsonl"
        session_file.parent.mkdir()

        with session_file.open(mode="w") as f:
            # Skill template injected as list-content - should be filtered
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "# Feature Development\n\n"
                                "You are helping a developer implement a new feature. "
                                "Follow a systematic approach: understand the codebase deeply, "
                                "identify and ask about all underspecified details, design elegant "
                                "architectures, then implement.\n\n## Core Principles\n..."
                            )
                        }
                    ]
                }
            }) + "\n")
            # Real user message (string content) - should be kept
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {
                    "role": "user",
                    "content": "Please implement the PMAT-999 feature as described"
                }
            }) + "\n")

        import time
        os.utime(session_file, (time.time(), time.time()))
        cutoff = datetime.now().strftime("%Y-%m-%d")

        result = extract_sessions_from_jsonl(
            projects_dir=str(projects_dir),
            cutoff=cutoff,
            target_repo=None,
        )

        assert len(result) == 1
        content = result[list(result.keys())[0]]["content"]
        # Real user message should be present
        assert "Please implement the PMAT-999 feature" in content
        # Skill template boilerplate should NOT appear
        assert "# Feature Development" not in content
        assert "You are helping a developer implement" not in content


class TestSynthesizeMemory:
    """Tests for synthesize_memory function."""

    @patch('dream.call_llm')
    def test_synthesize_returns_memory(self, mock_call_llm):
        """Test that synthesize_memory returns updated memory."""
        mock_call_llm.return_value = json.dumps({
            "content": [
                {"type": "text", "text": "# MEMORY.md — test-repo\n_Last updated: 2026-05-11_\n\n## Identity\n\n## Current State\n\n## Recent Signals\n\n## ⚠️ Conflicts\n"}
            ]
        })

        result = synthesize_memory(
            existing_memory="Old memory content",
            new_learnings="New learning",
            date="2026-05-11",
            repo_name="test-repo",
            model_config={"model": "test", "token": "test", "base_url": ""}
        )

        assert result is not None
        assert "MEMORY.md" in result
    @patch('dream.call_llm')
    def test_returns_none_on_llm_failure(self, mock_call_llm):
        """Test that None is returned when LLM call fails."""
        mock_call_llm.return_value = None

        result = synthesize_memory(
            existing_memory="Old memory content",
            new_learnings="New learning",
            date="2026-05-11",
            repo_name="test-repo",
            model_config={"model": "test", "token": "test", "base_url": ""}
        )

        assert result is None
        assert result is None


class TestConstants:
    """Tests for memory model constants."""

    def test_layer_headers_defined(self):
        """Test that section header constants are defined."""
        from dream import LAYER1_HEADER, LAYER2_HEADER, LAYER3_HEADER, CONFLICTS_HEADER
        assert LAYER1_HEADER == "## Identity"
        assert LAYER2_HEADER == "## Current State"
        assert LAYER3_HEADER == "## Recent Signals"
        assert CONFLICTS_HEADER == "## ⚠️ Conflicts"

    def test_routing_tags_defined(self):
        """Test that routing tag constants are defined."""
        from dream import REPO_LOCAL_TAG, GLOBAL_TAG
        assert REPO_LOCAL_TAG == "[REPO-LOCAL]"
        assert GLOBAL_TAG == "[GLOBAL]"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
class TestSplitRouting:
    """Tests for split_routing function."""

    def test_splits_repo_local_and_global(self):
        """Test basic splitting of REPO-LOCAL and GLOBAL insights."""
        extracted = """## Extracted Insights

### Layer1 — Durable Identity
- [REPO-LOCAL] Use trunk-based branching.
- [GLOBAL] Team uses JIRA for all tickets.

### Layer2 — Current State
- [REPO-LOCAL] Refactoring auth module (Expires: auth done).
- [GLOBAL] Team is migrating to Python 3.10 (Expires: migration done).

### Rejected Decisions
- Feature X — Reason: too complex."""

        repo_local, global_md = split_routing(extracted)

        assert "[REPO-LOCAL]" in repo_local
        assert "[GLOBAL]" not in repo_local
        assert "[GLOBAL]" in global_md
        assert "[REPO-LOCAL]" not in global_md

    def test_preserves_rejected_decisions_in_both(self):
        """Test that Rejected Decisions appear in both outputs."""
        extracted = """## Extracted Insights

### Rejected Decisions
- Feature X — Reason: too complex.
- Feature Y — Reason: not prioritized."""

        repo_local, global_md = split_routing(extracted)

        assert "Rejected Decisions" in repo_local
        assert "Rejected Decisions" in global_md
        assert "Feature X" in repo_local
        assert "Feature X" in global_md

    def test_empty_sections_filtered(self):
        """Test that empty sections are filtered out."""
        extracted = """## Extracted Insights

### Layer3 — Session Signals
<!-- Max 3 bullets. One sentence each. -->

### Rejected Decisions
- Decision — Reason."""

        repo_local, global_md = split_routing(extracted)

        # Layer3 has no content, so should be filtered
        assert "Session Signals" not in repo_local
        assert "Rejected Decisions" in repo_local


class TestEnforceRecentSignalsCap:
    """Tests for enforce_recent_signals_cap function."""

    def test_caps_entries_at_5(self):
        """Test that only last 5 entries are kept."""
        content = """# MEMORY.md — test-repo
_Last updated: 2026-05-11_

## Recent Signals
- Signal 1
- Signal 2
- Signal 3
- Signal 4
- Signal 5
- Signal 6
- Signal 7"""

        result = enforce_recent_signals_cap(content, max_entries=5)

        # Should have only last 5
        assert "Signal 1" not in result
        assert "Signal 2" not in result
        assert "Signal 6" in result
        assert "Signal 7" in result

    def test_no_cap_if_under_limit(self):
        """Test that no entries are removed if under cap."""
        content = """# MEMORY.md — test-repo
_Last updated: 2026-05-11_

## Recent Signals
- Signal 1
- Signal 2"""

        result = enforce_recent_signals_cap(content, max_entries=5)

        assert "Signal 1" in result
        assert "Signal 2" in result

    def test_cap_when_section_at_end(self):
        """Test cap works when Recent Signals is last section."""
        content = """# MEMORY.md — test-repo
_Last updated: 2026-05-11_

## Recent Signals
- Signal 1
- Signal 2
- Signal 3
- Signal 4
- Signal 5
- Signal 6"""

        result = enforce_recent_signals_cap(content, max_entries=5)

        assert "Signal 1" not in result
        assert "Signal 6" in result


class TestCheckConflicts:
    """Tests for check_conflicts function."""

    def test_detects_non_empty_conflicts(self):
        """Test that conflicts are detected when section has content."""
        content = """# MEMORY.md — test-repo
_Last updated: 2026-05-11_

## ⚠️ Conflicts
- [Date] repo: NEW vs EXISTING — awaiting resolution"""

        has_conflicts, conflicts = check_conflicts(content)

        assert has_conflicts is True
        assert len(conflicts) > 0

    def test_no_conflicts_when_section_empty(self):
        """Test that no conflicts when section exists but empty."""
        content = """# MEMORY.md — test-repo
_Last updated: 2026-05-11_

## ⚠️ Conflicts
<!-- Unresolved contradictions. Human review required. -->"""

        has_conflicts, conflicts = check_conflicts(content)

        assert has_conflicts is False
        assert len(conflicts) == 0

    def test_no_conflicts_when_section_missing(self):
        """Test that no conflicts when section doesn't exist."""
        content = """# MEMORY.md — test-repo
_Last updated: 2026-05-11_

## Identity
Some identity content."""

        has_conflicts, conflicts = check_conflicts(content)

        assert has_conflicts is False

class TestIsRealUserMessage:
    """Tests for is_real_user_message function."""

    def test_real_user_message_returns_true(self):
        """Test that a plain user message returns True."""
        text = "feature-dev:feature-dev please implement PMAT-182"
        assert is_real_user_message(text) is True

    def test_command_name_tag_returns_false(self):
        """Test that <command-name> tag returns False."""
        text = "<command-name>/exit</command-name>"
        assert is_real_user_message(text) is False

    def test_command_message_tag_returns_false(self):
        """Test that <command-message> tag returns False."""
        text = "<command-message>exit</command-message>"
        assert is_real_user_message(text) is False

    def test_system_reminder_tag_returns_false(self):
        """Test that <system-reminder> tag returns False."""
        text = "<system-reminder>This is a reminder</system-reminder>"
        assert is_real_user_message(text) is False

    def test_local_command_tag_returns_false(self):
        """Test that <local-command-*> tag returns False."""
        text = "<local-command-caveat>Some caveat text</local-command-caveat>"
        assert is_real_user_message(text) is False

    def test_local_command_stdout_returns_false(self):
        """Test that <local-command-stdout> tag returns False."""
        text = "<local-command-stdout>Goodbye!</local-command-stdout>"
        assert is_real_user_message(text) is False

    def test_empty_message_returns_false(self):
        """Test that empty message returns False."""
        assert is_real_user_message("") is False
        assert is_real_user_message(None) is False

    def test_message_with_multiple_tags_returns_false(self):
        """Test that message with multiple Claude Code tags returns False."""
        text = "<command-name>/rename</command-name>\n<command-message>rename</command-message>"
        assert is_real_user_message(text) is False

    def test_partial_tag_match_returns_false(self):
        """Test that partial tag match (e.g., <system-reminder with attrs) returns False."""
        text = "<system-reminder name=\"test\">Some reminder</system-reminder>"
        assert is_real_user_message(text) is False
    def test_context_usage_table_returns_false(self):
        """Test that Context Usage tables are filtered out."""
        text = """## Context Usage

**Model:** tencent/hy3-preview
**Tokens:** 63.2k / 200k (32%)

### Estimated usage by category

| Category | Tokens | Percentage |
|----------|---------|------------|
| System prompt | 6.2k | 3.1% |"""
        assert is_real_user_message(text) is False

    def test_session_continuation_summary_returns_false(self):
        """Test that session continuation summaries are filtered out."""
        text = """This session is being continued from a previous conversation that ran out of context.

Summary:
1. Primary Request and Intent:
   The user reported a bug..."""
        assert is_real_user_message(text) is False

    def test_task_notification_returns_false(self):
        """Test that <task-notification> harness messages are filtered out."""
        text = (
            "<task-notification>\n"
            "<task-id>bawb2702u</task-id>\n"
            "<tool-use-id>call_function_5j3o338jd48m_1</tool-use-id>\n"
            "<output-file>/tmp/claude-1000/tasks/bawb2702u.output</output-file>\n"
            "<status>killed</status>\n"
            "</task-notification>"
        )
        assert is_real_user_message(text) is False

    def test_any_xml_tag_start_returns_false(self):
        """Test that any message starting with a well-formed XML tag is filtered."""
        assert is_real_user_message("<unknown-harness-tag>content</unknown-harness-tag>") is False
        assert is_real_user_message("<foo>bar</foo>") is False

    def test_non_xml_angle_brackets_not_filtered(self):
        """Test that <3 and <-- (not XML tags) are NOT filtered."""
        assert is_real_user_message("<3 this is a real message") is True
        assert is_real_user_message("<-- comment style message here") is True

    def test_user_message_mentioning_xml_not_filtered(self):
        """Test that a real message that CONTAINS (but doesn't start with) an XML tag is kept."""
        text = "I saw this error: <task-notification> appeared in my dream output"
        assert is_real_user_message(text) is True

    def test_skill_instructions_string_content_returns_false(self):
        """Test that skill instruction blocks (string content, e.g. search-company-knowledge) are filtered."""
        text = "Base directory for this skill: /home/ec2-user/.claude/plugins/cache/atlassian\n\n# Search Company Knowledge\n\n## Keywords\nfind information, search company knowledge..."
        assert is_real_user_message(text) is False

    def test_request_interrupted_returns_false(self):
        """Test that Claude Code interruption notices are filtered out."""
        assert is_real_user_message("[Request interrupted by user]") is False
        assert is_real_user_message("[Request interrupted by user for tool use]") is False

    def test_localhost_oauth_callback_returns_false(self):
        """Test that localhost OAuth callback URLs are filtered out."""
        text = "http://localhost:60440/callback?code=712020-abc&state=xyz"
        assert is_real_user_message(text) is False

