"""
Tests for dream_core module.
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

from dream_core import (
    extract_sessions_from_jsonl,
    extract_response_text,
    extract_learnings,
    synthesize_memory,
    create_new_memory,
    MAX_MEMORY_LINES,
    CROSS_REPO_TAG,
    split_routing,
    enforce_recent_signals_cap,
    check_conflicts,
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
        repo1_session.write_text(json.dumps({
            "cwd": "/home/user/repos/repo1",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "A" * 150}]}
        }) + "\n")

        repo2_session = projects_dir / "repo2-session" / "session.jsonl"
        repo2_session.parent.mkdir()
        repo2_session.write_text(json.dumps({
            "cwd": "/home/user/repos/repo2",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "B" * 150}]}
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

        # Write valid and invalid lines using open with append
        with session_file.open(mode="w") as f:
            f.write(json.dumps({
                "cwd": "/home/user/repos/test",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "A" * 150}]}
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


class TestSynthesizeMemory:
    """Tests for synthesize_memory function."""

    @patch('dream_core.call_llm')
    def test_extracts_cross_repo_items(self, mock_call_llm):
        """Test that CROSS-REPO tagged lines are extracted."""
        mock_call_llm.return_value = json.dumps({
            "content": [
                {"type": "text", "text": "Regular learning\n**[CROSS-REPO]** This applies everywhere\nAnother point"}
            ]
        })

        result, cross_items = synthesize_memory(
            existing_memory="Old memory content",
            new_learnings="New learning",
            date="2026-05-11",
            repo_name="test-repo",
            model_config={"model": "test", "token": "test", "base_url": ""}
        )

        assert len(cross_items) == 1
        assert "[CROSS-REPO]" in cross_items[0]

    @patch('dream_core.call_llm')
    def test_returns_none_on_llm_failure(self, mock_call_llm):
        """Test that None is returned when LLM call fails."""
        mock_call_llm.return_value = None

        result, cross_items = synthesize_memory(
            existing_memory="Old memory content",
            new_learnings="New learning",
            date="2026-05-11",
            repo_name="test-repo",
            model_config={"model": "test", "token": "test", "base_url": ""}
        )

        assert result is None
        assert cross_items == []


class TestCrossRepoTag:
    """Tests for CROSS_REPO_TAG constant."""

    def test_cross_repo_tag_is_defined(self):
        """Test that CROSS_REPO_TAG is properly defined."""
        assert CROSS_REPO_TAG == "[CROSS-REPO]"
        assert len(CROSS_REPO_TAG) > 0


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
