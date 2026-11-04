"""Acceptance tests for the eval case miner (PMAT-300, PMAT-301, PMAT-307, PMAT-313, PMAT-309).

The canned reports are made by the review's own render_report, so the parser is tested
against the exact text `mesh review-pull-request` prints and posts. gh and mesh are fakes
that record every call: nothing reaches GitHub or a model.
"""

import json
import time
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

    def __init__(self, prs, comments=None, commits=None, files=None):
        self.prs, self.comments, self.calls = prs, comments or {}, []
        self.files = files or {}  # raised sha -> changed files in the diff base...raised
        self.commits = commits or {}  # PR number -> [{"sha", "message"}], oldest first

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["pr", "list"]:
            return json.dumps(self.prs)
        if args[0] == "api" and args[1].endswith("/comments"):  # repos/<owner>/<repo>/issues/<n>/comments
            return "".join(json.dumps(body) + "\n" for body in self.comments.get(int(args[1].split("/")[4]), []))
        if args[0] == "api" and args[1].endswith("/commits"):  # repos/<owner>/<repo>/pulls/<n>/commits
            return "".join(json.dumps(c) + "\n" for c in self.commits.get(int(args[1].split("/")[4]), []))
        if args[0] == "api" and "/compare/" in args[1]:  # repos/<owner>/<repo>/compare/<base>...<head>
            return f"{self.files[args[1].rsplit('...', 1)[1]]}\n"
        raise AssertionError(f"unexpected gh call: {args}")


def mine(tmp_path, gh, **kw):
    return eval_cases.mine(REPO, limit=10, gh=gh, cases_dir=tmp_path / "cases", today=TODAY, **kw)


def repo_cases(tmp_path):
    return {p.name: json.loads(p.read_text()) for p in sorted((tmp_path / "cases" / "repo").glob("*.json"))}


FIX1 = "1" * 40
MEDIUM = Issue("src/a.py", 12, "medium", "Index can go out of range.", "Check the length first.")
LOW = Issue("src/b.py", 3, "low", "The name is unclear.", None)
LEGACY = {"case_id": "fad6e3371bb2", "repo": REPO, "pr_number": 1,
          "finding": {"file": "src/a.py", "line": 12, "severity": "medium", "id": None}, "label": "false_positive",
          "label_by": "file_rule", "label_date": "2026-10-27", "label_reason": "Raised in review and merged with the file unchanged."}


def write_legacy(tmp_path):
    path = tmp_path / "cases" / "legacy-file-rule" / "repo-pr1-fad6e3371bb2.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(LEGACY, indent=2) + "\n")
    return path


def test_a_rerun_keeps_a_finding_line_fixed_by_hand(tmp_path):
    gh = FakeGh([pr(7, BASE1, HEAD1)],
                comments={7: [report(OLD1, [MEDIUM], pr_number=7), "Dismissed review finding R7-eeeeeee-001: not a bug."]},
                commits={7: []})
    mine(tmp_path, gh)
    path = tmp_path / "cases" / "repo" / "R7-eeeeeee-001.json"
    case = json.loads(path.read_text())
    case["finding"]["line"] = 25  # the label fix: the review cited the wrong line
    path.write_text(json.dumps(case, indent=2) + "\n")

    mine(tmp_path, gh)

    assert json.loads(path.read_text())["finding"]["line"] == 25


def test_max_files_leaves_out_findings_raised_on_a_large_commit_and_asks_github_once_per_commit(tmp_path):
    small, large = "c" * 40, "d" * 40
    issues = [MEDIUM, Issue("src/b.py", 20, "medium", "The caller may pass None.", None)]
    gh = FakeGh([pr(7, BASE1, HEAD1)],
                comments={7: [report(small, issues, pr_number=7), report(large, issues, pr_number=7),
                              "Dismissed review finding R7-ccccccc-001: not a bug.\n"
                              "Dismissed review finding R7-ccccccc-002: not a bug.\n"
                              "Dismissed review finding R7-ddddddd-001: not a bug.\n"
                              "Dismissed review finding R7-ddddddd-002: not a bug."]},
                commits={7: []}, files={small: 22, large: 23})

    summary = mine(tmp_path, gh, max_files=22)

    assert sorted(repo_cases(tmp_path)) == ["R7-ccccccc-001.json", "R7-ccccccc-002.json"]
    assert summary["findings"]["too_large"] == 2 and summary["findings"]["dismissed"] == 2
    compares = [args[1] for args in gh.calls if args[0] == "api" and "/compare/" in args[1]]
    assert sorted(compares) == [f"repos/{REPO}/compare/{BASE1}...{small}", f"repos/{REPO}/compare/{BASE1}...{large}"]


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
                              "Dismissed review finding R7-eeeeeee-006: the name is fine."]},
                commits={7: [commit(OLD1, "feature[PMAT-1]: first cut"),
                             commit(FIX1, "fix[PMAT-1]: fixed review findings R7-eeeeeee-001 and R7-eeeeeee-004")]})

    summary = mine(tmp_path, gh)

    files = repo_cases(tmp_path)
    assert sorted(files) == ["R7-eeeeeee-001.json", "R7-eeeeeee-002.json", "R7-eeeeeee-006.json"]  # low included
    assert files["R7-eeeeeee-006.json"]["finding"]["severity"] == "low"
    real, false = files["R7-eeeeeee-001.json"], files["R7-eeeeeee-002.json"]
    assert {k: real[k] for k in ("case_id", "repo", "pr_number", "base_sha", "head_sha", "raised_sha", "fix_sha",
                                 "label", "label_by")} == {
        "case_id": "R7-eeeeeee-001", "repo": REPO, "pr_number": 7, "base_sha": BASE1, "head_sha": HEAD1,
        "raised_sha": OLD1, "fix_sha": FIX1, "label": "real_issue", "label_by": "rule"}
    assert "times_raised" not in real
    assert {k: false[k] for k in ("case_id", "raised_sha", "fix_sha", "label", "label_by", "label_reason")} == {
        "case_id": "R7-eeeeeee-002", "raised_sha": OLD1, "fix_sha": None, "label": "false_positive",
        "label_by": "rule", "label_reason": "the caller never passes None (src/x.py:40)."}
    assert summary["cases_written"] == 3
    assert summary["findings"] == {"cited": 1, "dismissed": 2, "duplicate": 1, "unresolved": 1, "conflict": 1,
                                   "no_id": 0, "no_line": 0, "too_large": 0}
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
                                   "no_id": 1, "no_line": 0, "too_large": 0}


def test_a_finding_without_a_line_gives_no_case(tmp_path):
    """A case is matched by its line at raised_sha (PMAT-304), so a finding without one cannot be a case."""
    no_line = Issue("src/a.py", None, "medium", "Index can go out of range.", None)
    gh = FakeGh([pr(7, BASE1, HEAD1)],
                comments={7: [report(HEAD1, [no_line], pr_number=7),
                              "Dismissed review finding R7-bbbbbbb-001: not a bug."]})

    summary = mine(tmp_path, gh)

    assert repo_cases(tmp_path) == {}
    assert summary["findings"] == {"cited": 0, "dismissed": 0, "duplicate": 0, "unresolved": 0, "conflict": 0,
                                   "no_id": 0, "no_line": 1, "too_large": 0}


def test_a_later_dismissal_of_the_same_id_corrects_the_earlier_one(tmp_path):
    original = Issue("src/a.py", 12, "medium", "Index can go out of range.", None)
    repeat = Issue("src/a.py", 13, "medium", "Index can go out of range here too.", None)
    gh = FakeGh([pr(7, BASE1, HEAD1)],
                comments={7: [report(HEAD1, [original, repeat], pr_number=7),
                              "Dismissed review finding R7-bbbbbbb-001: checked by the caller.\n"
                              "Dismissed review finding R7-bbbbbbb-002: same claim as R7-bbbbbbb-001.",
                              "Dismissed review finding R7-bbbbbbb-002: duplicate of R7-bbbbbbb-001"]})

    summary = mine(tmp_path, gh)

    assert sorted(repo_cases(tmp_path)) == ["R7-bbbbbbb-001.json"]
    assert (summary["findings"]["dismissed"], summary["findings"]["duplicate"]) == (1, 1)


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


def test_a_rerun_leaves_unchanged_cases_untouched_and_redates_only_a_changed_label(tmp_path):  # PMAT-313
    cited = Issue("src/a.py", 12, "high", "Index can go out of range.", "Check the length first.")
    dismissed = Issue("src/b.py", 20, "medium", "The caller may pass None.", None)
    comments = {7: [report(OLD1, [cited, dismissed], pr_number=7),
                    "Dismissed review finding R7-eeeeeee-002: the caller never passes None."]}
    commits = {7: [commit(FIX1, "fix[PMAT-1]: fixed review finding R7-eeeeeee-001")]}
    gh = FakeGh([pr(7, BASE1, HEAD1)], comments=comments, commits=commits)
    folder = tmp_path / "cases" / "repo"
    later = date(2026, 11, 3)
    mine(tmp_path, gh)
    before = {p.name: p.read_bytes() for p in folder.glob("*.json")}

    eval_cases.mine(REPO, limit=10, gh=gh, cases_dir=tmp_path / "cases", today=later)

    assert {p.name: p.read_bytes() for p in folder.glob("*.json")} == before

    comments[7].append("Dismissed review finding R7-eeeeeee-002: checked at src/x.py:40.")
    eval_cases.mine(REPO, limit=10, gh=gh, cases_dir=tmp_path / "cases", today=later)

    files = repo_cases(tmp_path)
    assert (folder / "R7-eeeeeee-001.json").read_bytes() == before["R7-eeeeeee-001.json"]
    changed = files["R7-eeeeeee-002.json"]
    assert (changed["label_reason"], changed["label_date"]) == ("checked at src/x.py:40.", later.isoformat())


def test_mined_cases_get_category_notes_and_an_expected_block_derived_from_the_label(tmp_path):  # PMAT-309 (a)
    cited = Issue("src/a.py", 12, "high", "Index can go out of range.", "Check the length first.")
    dismissed = Issue("src/b.py", 20, "medium", "The caller may pass None.", None)
    gh = FakeGh([pr(7, BASE1, HEAD1)],
                comments={7: [report(OLD1, [cited, dismissed], pr_number=7),
                              "Dismissed review finding R7-eeeeeee-002: the caller never passes None."]},
                commits={7: [commit(FIX1, "fix[PMAT-1]: fixed review finding R7-eeeeeee-001")]})

    mine(tmp_path, gh)

    files = repo_cases(tmp_path)
    real, false = files["R7-eeeeeee-001.json"], files["R7-eeeeeee-002.json"]
    assert (real["category"], real["notes"]) == (false["category"], false["notes"]) == ("mined", "")
    assert real["expected"] == {"action": "raise_true_bug", "flag_file": "src/a.py", "min_severity": "high"}
    assert false["expected"] == {"action": "skip_false_bug", "flag_file": "src/b.py"}


def test_a_rerun_keeps_a_hand_edited_expected_and_notes_and_label_recomputes_expected(tmp_path):  # PMAT-309 (c)
    gh = FakeGh([pr(7, BASE1, HEAD1)],
                comments={7: [report(HEAD1, [MEDIUM], pr_number=7),
                              "Dismissed review finding R7-bbbbbbb-001: checked by the caller."]})
    mine(tmp_path, gh)
    path = tmp_path / "cases" / "repo" / "R7-bbbbbbb-001.json"
    case = json.loads(path.read_text())
    hand = {"action": "raise_nothing", "flag_file": "src/*.py", "min_severity": "medium"}
    case.update(expected=hand, notes="The caller checks the length at src/x.py:40.")
    path.write_text(json.dumps(case, indent=2) + "\n")

    mine(tmp_path, gh)

    kept = json.loads(path.read_text())
    assert (kept["expected"], kept["notes"]) == (hand, "The caller checks the length at src/x.py:40.")

    eval_cases.label("R7-bbbbbbb-001", "real_issue", "The caller never checks the length.",
                     cases_dir=tmp_path / "cases", today=TODAY)

    relabelled = json.loads(path.read_text())
    assert relabelled["expected"] == {"action": "raise_true_bug", "flag_file": "src/a.py", "min_severity": "medium"}
    assert relabelled["notes"] == "The caller checks the length at src/x.py:40."


def test_a_hostile_comment_line_cannot_stall_the_parser():
    """Anyone who can comment on a PR writes what mine parses: a long run of backticks took cubic time."""
    ticks = "`" * 6000
    started = time.monotonic()
    parsed = eval_cases.parse_report(f"# AI Code Review\n> **Model:** {ticks}x\n\n### 🔴 High\n\n#### {ticks} x\n"
                                     f"#### `a` · model: {ticks}x\n")
    assert time.monotonic() - started < 5
    assert parsed["model"] is None
