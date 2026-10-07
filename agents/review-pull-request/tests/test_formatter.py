"""Tests for formatter.py — render_report."""
import pytest

from review_pull_request.formatter import render_report, SEVERITY_EMOJI
from review_pull_request.stage2 import Issue


class MockConfig:
    model = "deepseek/deepseek-v4-flash"
    overview_model = ""
    base_branch = "master"

    @property
    def effective_overview_model(self):
        return self.overview_model or self.model


def test_renders_high_severity_issue():
    issues = [
        Issue(
            file="src/auth.py",
            line=42,
            severity="high",
            comment="Timing attack vulnerability.",
            suggestion="Use hmac.compare_digest().",
        )
    ]
    config = MockConfig()
    report = render_report("overview text", issues, [], config)
    assert "🔴 High" in report
    assert "src/auth.py" in report
    assert "line 42" in report
    assert "Timing attack" in report
    assert "hmac.compare_digest" in report


def test_renders_medium_severity_issue():
    issues = [
        Issue(
            file="src/api.py",
            line=88,
            severity="medium",
            comment="SQL injection risk.",
            suggestion="Use parameterized queries.",
        )
    ]
    config = MockConfig()
    report = render_report("overview", issues, [], config)
    assert "🟡 Medium" in report


def test_renders_low_severity_issue():
    issues = [
        Issue(
            file="src/main.py",
            line=10,
            severity="low",
            comment="Minor issue.",
            suggestion="Minor fix.",
        )
    ]
    config = MockConfig()
    report = render_report("overview", issues, [], config)
    assert "🟢 Low" in report


def test_sorts_by_severity_high_first():
    issues = [
        Issue(file="b.py", line=1, severity="low", comment="Low", suggestion="Fix low."),
        Issue(file="a.py", line=1, severity="high", comment="High", suggestion="Fix high."),
        Issue(file="c.py", line=1, severity="medium", comment="Med", suggestion="Fix med."),
    ]
    config = MockConfig()
    report = render_report("overview", issues, [], config)
    high_pos = report.index("High")
    med_pos = report.index("Medium")
    low_pos = report.index("Low")
    assert high_pos < med_pos < low_pos, "Severity should be ordered high → medium → low"


def test_empty_issues_no_problems_found():
    config = MockConfig()
    report = render_report("overview", [], [], config)
    assert "No issues found" in report
    assert "✅" in report


def test_missing_severity_group_omitted():
    """High and low present, medium absent — medium section should not appear."""
    issues = [
        Issue(file="a.py", line=1, severity="high", comment="High", suggestion="Fix."),
        Issue(file="b.py", line=2, severity="low", comment="Low", suggestion="Fix."),
    ]
    config = MockConfig()
    report = render_report("overview", issues, [], config)
    assert "🔴 High" in report
    assert "🟡 Medium" not in report
    assert "🟢 Low" in report


def test_file_paths_in_backticks():
    issues = [
        Issue(file="src/auth.py", line=5, severity="high", comment="Bug.", suggestion="Fix."),
    ]
    config = MockConfig()
    report = render_report("overview", issues, [], config)
    assert "`src/auth.py`" in report
    assert "`" in report


def test_incomplete_files_suppress_the_all_clear_and_are_listed():
    from review_pull_request.config import Config

    cfg = Config(model="m", api_url="http://x", api_token="t")
    body = render_report("ov", [], summaries=[("a.py", "s")], config=cfg, model="m",
                         incomplete=["b.py", "c.py"])

    assert "No issues found. ✅" not in body
    assert "not fully reviewed" in body.lower()
    assert "b.py" in body and "c.py" in body


def test_header_counts_only_files_that_were_actually_reviewed():
    from review_pull_request.config import Config

    cfg = Config(model="m", api_url="http://x", api_token="t")
    body = render_report("ov", [], summaries=[("a.py", "s"), ("b.py", "s"), ("c.py", "s")],
                         config=cfg, model="m", incomplete=["c.py"])

    assert "Files reviewed:** 2 of 3" in body


def test_excluded_files_are_listed_so_the_report_does_not_imply_full_coverage():
    from review_pull_request.config import Config

    cfg = Config(model="m", api_url="http://x", api_token="t")
    body = render_report("ov", [], summaries=[("a.py", "s")], config=cfg, model="m",
                         excluded=["README.md", "CLAUDE.md"])

    assert "Excluded from review" in body
    assert "README.md" in body and "CLAUDE.md" in body
