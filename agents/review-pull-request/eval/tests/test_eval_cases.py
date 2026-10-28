"""Acceptance tests for the eval case miner (PMAT-300, PMAT-301, PMAT-307).

The canned reports are made by the review's own render_report, so the parser is tested
against the exact text `mesh review-pull-request` prints and posts. gh and mesh are fakes
that record every call: nothing reaches GitHub or a model.
"""

import json
from datetime import date
from types import SimpleNamespace

import pytest

import eval_cases
from review_pull_request.formatter import render_report
from review_pull_request.stage2 import Issue

REPO = "org/repo"
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
BASE1, HEAD1, OLD1 = "a" * 40, "b" * 40, "e" * 40
TODAY = date(2026, 10, 27)


def url(n):
    return f"https://github.com/{REPO}/pull/{n}"


def pr(n, base, head):
    return {"number": n, "title": f"PR {n}", "baseRefOid": base, "headRefOid": head, "url": url(n)}


def report(head_sha, issues, model=MODEL, pr_number=None):
    config = SimpleNamespace(base_branch="master", model=model)
    return render_report(overview="Overview.", issues=issues, summaries=[(i.file, "s") for i in issues],
                         config=config, model=model, head_sha=head_sha, pr_number=pr_number,
                         file_models={i.file: model for i in issues}, issue_models=[model for _ in issues])


def commit(sha, message):
    return {"sha": sha, "message": message}


class FakeGh:
    """Reads only: merged PRs, a PR's comments and its commits. Any other call fails the test."""

    def __init__(self, prs, comments=None, commits=None):
        self.prs, self.comments, self.calls = prs, comments or {}, []
        self.commits = commits or {}  # PR number -> [{"sha", "message"}], oldest first

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["pr", "list"]:
            return json.dumps(self.prs)
        if args[0] == "api" and args[1].endswith("/comments"):  # repos/<owner>/<repo>/issues/<n>/comments
            return "".join(json.dumps(body) + "\n" for body in self.comments.get(int(args[1].split("/")[4]), []))
        if args[0] == "api" and args[1].endswith("/commits"):  # repos/<owner>/<repo>/pulls/<n>/commits
            return "".join(json.dumps(c) + "\n" for c in self.commits.get(int(args[1].split("/")[4]), []))
        raise AssertionError(f"unexpected gh call: {args}")


def mine(tmp_path, gh, **kw):
    return eval_cases.mine(REPO, limit=10, gh=gh, cases_dir=tmp_path / "cases", today=TODAY, **kw)


def repo_cases(tmp_path):
    return {p.name: json.loads(p.read_text()) for p in sorted((tmp_path / "cases" / "repo").glob("*.json"))}


FIX1 = "1" * 40
MEDIUM = Issue("src/a.py", 12, "medium", "Index can go out of range.", "Check the length first.")
LOW = Issue("src/b.py", 3, "low", "The name is unclear.", None)
LEGACY = {"case_id": "fad6e3371bb2", "repo": REPO, "pr_number": 1, "label": "false_positive",
          "label_by": "file_rule", "label_date": "2026-10-27", "label_reason": "Raised in review and merged with the file unchanged."}


def write_legacy(tmp_path):
    path = tmp_path / "cases" / "legacy-file-rule" / "repo-pr1-fad6e3371bb2.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(LEGACY, indent=2) + "\n")
    return path


def test_each_finding_id_is_labelled_by_what_happened_to_it(tmp_path):  # PMAT-307 acceptance test
    cited = Issue("src/a.py", 12, "high", "Index can go out of range.", "Check the length first.")
    dismissed = Issue("src/b.py", 20, "medium", "The caller may pass None.", None)
    duplicate = Issue("src/a.py", 14, "medium", "Index can go out of range again.", None)
    both = Issue("src/c.py", 5, "medium", "Cited and dismissed.", None)
    neither = Issue("src/d.py", 9, "high", "Never resolved.", None)
    legacy = write_legacy(tmp_path)
    legacy_before = legacy.read_bytes()
    gh = FakeGh([pr(7, BASE1, HEAD1)],
                comments={7: [report(OLD1, [cited, dismissed, duplicate, both, neither, LOW], pr_number=7),
                              "Review loop done.\n"
                              "Dismissed review finding R7-eeeeeee-002: the caller never passes None (src/x.py:40).\n"
                              "Dismissed review finding R7-eeeeeee-003: duplicate of R7-eeeeeee-001\n"
                              "Dismissed review finding R7-eeeeeee-004: not a bug.\n"
                              "Dismissed review finding R7-eeeeeee-006: low findings never become cases."]},
                commits={7: [commit(OLD1, "feature[PMAT-1]: first cut"),
                             commit(FIX1, "fix[PMAT-1]: fixed review findings R7-eeeeeee-001 and R7-eeeeeee-004")]})

    summary = mine(tmp_path, gh)

    files = repo_cases(tmp_path)
    assert sorted(files) == ["R7-eeeeeee-001.json", "R7-eeeeeee-002.json"]
    real, false = files["R7-eeeeeee-001.json"], files["R7-eeeeeee-002.json"]
    assert {k: real[k] for k in ("case_id", "repo", "pr_number", "base_sha", "head_sha", "raised_sha", "fix_sha",
                                 "label", "label_by")} == {
        "case_id": "R7-eeeeeee-001", "repo": REPO, "pr_number": 7, "base_sha": BASE1, "head_sha": HEAD1,
        "raised_sha": OLD1, "fix_sha": FIX1, "label": "real_issue", "label_by": "rule"}
    assert "times_raised" not in real
    assert {k: false[k] for k in ("case_id", "raised_sha", "fix_sha", "label", "label_by", "label_reason")} == {
        "case_id": "R7-eeeeeee-002", "raised_sha": OLD1, "fix_sha": None, "label": "false_positive",
        "label_by": "rule", "label_reason": "the caller never passes None (src/x.py:40)."}
    assert summary["cases_written"] == 2
    assert summary["findings"] == {"cited": 1, "dismissed": 1, "duplicate": 1, "unresolved": 1, "conflict": 1,
                                   "no_id": 0}
    assert legacy.read_bytes() == legacy_before
    write_flags = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
    for args in gh.calls:
        assert args[:2] == ["pr", "list"] or args[0] == "api", args
        assert "/compare/" not in args[1], args
        assert not write_flags.intersection(args), args


def test_a_finding_without_an_id_and_another_prs_dismissal_give_no_case(tmp_path):
    gh = FakeGh([pr(7, BASE1, HEAD1)],
                comments={7: [report(OLD1, [MEDIUM]),  # before PMAT-305: no ID
                              report(HEAD1, [MEDIUM], pr_number=7),
                              "Dismissed review finding R8-bbbbbbb-001: another PR's ID.\n"
                              "  Dismissed review finding R7-bbbbbbb-001: not at the start of the line."]})

    summary = mine(tmp_path, gh)

    assert repo_cases(tmp_path) == {}
    assert summary["findings"] == {"cited": 0, "dismissed": 0, "duplicate": 0, "unresolved": 1, "conflict": 0,
                                   "no_id": 1}


def test_mining_runs_nothing_but_gh_and_mining_again_keeps_a_judges_override(tmp_path, monkeypatch):
    def no_process(*args, **kwargs):
        raise AssertionError(f"mining must run no process (no review, no model): {args}")
    monkeypatch.setattr(eval_cases.subprocess, "run", no_process)
    gh = FakeGh([pr(7, BASE1, HEAD1)],
                comments={7: [report(HEAD1, [MEDIUM], pr_number=7),
                              "Dismissed review finding R7-bbbbbbb-001: checked by the caller."]})
    mine(tmp_path, gh)
    eval_cases.label("R7-bbbbbbb-001", "real_issue", "The caller never checks the length.",
                     cases_dir=tmp_path / "cases", by="claude-code", today=TODAY)

    mine(tmp_path, gh)

    case = repo_cases(tmp_path)["R7-bbbbbbb-001.json"]
    assert (case["label"], case["label_by"]) == ("real_issue", "claude-code")
    with pytest.raises(ValueError):
        eval_cases.label("R7-bbbbbbb-001", "unlabelled", "x", cases_dir=tmp_path / "cases")


def test_label_finds_a_legacy_case_and_rewrites_it_in_place(tmp_path):
    legacy = write_legacy(tmp_path)

    eval_cases.label("fad6e3371bb2", "real_issue", "A real bug after all.", cases_dir=tmp_path / "cases",
                     by="claude-code", today=TODAY)

    assert [p.name for p in (tmp_path / "cases").rglob("*.json")] == [legacy.name]
    case = json.loads(legacy.read_text())
    assert (case["label"], case["label_by"], case["label_reason"]) == ("real_issue", "claude-code",
                                                                       "A real bug after all.")
    assert [c["case_id"] for c in eval_cases.load_cases(tmp_path / "cases")] == ["fad6e3371bb2"]


@pytest.mark.parametrize("repo", ["org/repo/../../users/x", "org", "-X/POST", "org/repo extra", "org/re;po"])
def test_a_repo_that_is_not_owner_slash_name_is_refused_before_any_gh_call(tmp_path, repo):
    gh = FakeGh([])

    with pytest.raises(ValueError, match="owner/name"):
        eval_cases.mine(repo, limit=10, gh=gh, cases_dir=tmp_path / "cases", today=TODAY)

    assert gh.calls == []


def test_the_parser_reads_each_findings_id_and_an_old_report_without_ids_as_before():  # PMAT-305
    new = eval_cases.parse_report(report(HEAD1, [MEDIUM, LOW], pr_number=31))
    assert [(f["id"], f["file"], f["line"], f["model"]) for f in new["findings"]] == [
        ("R31-bbbbbbb-001", "src/a.py", 12, MODEL), ("R31-bbbbbbb-002", "src/b.py", 3, MODEL)]
    old = eval_cases.parse_report(report(HEAD1, [MEDIUM]))
    assert old["findings"] == [{"file": "src/a.py", "line": 12, "severity": "medium", "summary": MEDIUM.comment,
                                "fix": MEDIUM.suggestion, "failure_scenario": None, "model": MODEL, "id": None}]
