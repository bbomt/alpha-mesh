"""Unit tests for reviewer module."""

import pytest
from pr_review.reviewer import parse_review_output, Review, InlineComment, ReviewParseError


class TestParseReviewOutput:
    """Tests for parse_review_output function."""

    def test_valid_json(self):
        """Test parsing valid JSON output."""
        json_output = '''{
  "summary": "This is a great PR!",
  "inline_comments": [],
  "verdict": "approved"
}'''
        review = parse_review_output(json_output)
        assert review.summary == "This is a great PR!"
        assert review.verdict == "approved"
        assert len(review.inline_comments) == 0

    def test_json_with_prose_before_after(self):
        """Test parsing JSON embedded in prose."""
        output = """Here is my review:

```json
{
  "summary": "Looks good",
  "inline_comments": [],
  "verdict": "approved"
}
```

Hope this helps!"""
        review = parse_review_output(output)
        assert review.summary == "Looks good"
        assert review.verdict == "approved"

    def test_with_inline_comments(self):
        """Test parsing inline comments."""
        json_output = '''{
  "summary": "A few issues found",
  "inline_comments": [
    {
      "file": "src/main.py",
      "line": 42,
      "severity": "error",
      "comment": "Null pointer risk"
    },
    {
      "file": "src/utils.py",
      "line": 100,
      "severity": "warning",
      "comment": "Consider refactoring"
    }
  ],
  "verdict": "changes_requested"
}'''
        review = parse_review_output(json_output)
        assert len(review.inline_comments) == 2
        assert review.inline_comments[0].file == "src/main.py"
        assert review.inline_comments[0].line == 42
        assert review.inline_comments[0].severity == "error"
        assert review.verdict == "changes_requested"

    def test_excludes_lockfiles(self):
        """Test that lockfiles are excluded from inline comments."""
        json_output = '''{
  "summary": "Review with lockfile comment",
  "inline_comments": [
    {
      "file": "package-lock.json",
      "line": 5,
      "severity": "warning",
      "comment": "Don't edit lockfiles"
    },
    {
      "file": "src/main.py",
      "line": 10,
      "severity": "error",
      "comment": "Real issue"
    }
  ],
  "verdict": "comment"
}'''
        review = parse_review_output(json_output)
        # The lockfile comment should be filtered out
        assert len(review.inline_comments) == 1
        assert review.inline_comments[0].file == "src/main.py"

    def test_excludes_test_files(self):
        """Test that test files are excluded."""
        json_output = '''{
  "summary": "Review with test file comment",
  "inline_comments": [
    {
      "file": "tests/test_main.py",
      "line": 15,
      "severity": "suggestion",
      "comment": "Test comment"
    },
    {
      "file": "src/main.py",
      "line": 20,
      "severity": "error",
      "comment": "Real issue"
    }
  ],
  "verdict": "comment"
}'''
        review = parse_review_output(json_output)
        assert len(review.inline_comments) == 1
        assert review.inline_comments[0].file == "src/main.py"

    def test_excludes_config_files(self):
        """Test that config files are excluded."""
        json_output = '''{
  "summary": "Review with config comments",
  "inline_comments": [
    {
      "file": "config/settings.yaml",
      "line": 5,
      "severity": "warning",
      "comment": "Config issue"
    },
    {
      "file": "pyproject.toml",
      "line": 10,
      "severity": "warning",
      "comment": "TOML issue"
    },
    {
      "file": "src/main.py",
      "line": 20,
      "severity": "error",
      "comment": "Real issue"
    }
  ],
  "verdict": "comment"
}'''
        review = parse_review_output(json_output)
        assert len(review.inline_comments) == 1
        assert review.inline_comments[0].file == "src/main.py"

    def test_invalid_json(self):
        """Test that invalid JSON raises ReviewParseError."""
        with pytest.raises(ReviewParseError):
            parse_review_output("This is not JSON at all")

    def test_empty_output(self):
        """Test that empty output raises ReviewParseError."""
        with pytest.raises(ReviewParseError):
            parse_review_output("")

    def test_json_no_closing_brace(self):
        """Test handling of incomplete JSON."""
        with pytest.raises(ReviewParseError):
            parse_review_output('{"summary": "incomplete')

    def test_default_verdict(self):
        """Test that missing verdict defaults to 'comment'."""
        json_output = '''{
  "summary": "No verdict specified"
}'''
        review = parse_review_output(json_output)
        assert review.verdict == "comment"

    def test_severity_normalized(self):
        """Test that invalid severity values are normalized."""
        json_output = '''{
  "summary": "Test",
  "inline_comments": [
    {
      "file": "src/main.py",
      "line": 10,
      "severity": "invalid_severity",
      "comment": "Test"
    }
  ]
}'''
        review = parse_review_output(json_output)
        assert review.inline_comments[0].severity == "suggestion"

    def test_json_fence_with_nested_braces_in_summary(self):
        """Test parsing JSON in a fence where summary contains nested braces."""
        output = """Here is my review:

```json
{
  "summary": "This PR has {nested} braces in the {summary} text that should not break parsing",
  "inline_comments": [],
  "verdict": "approved"
}
```

Let me know if you have questions!"""
        review = parse_review_output(output)
        assert review.summary == "This PR has {nested} braces in the {summary} text that should not break parsing"
        assert review.verdict == "approved"
