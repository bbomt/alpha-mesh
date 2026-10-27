"""Acceptance tests for the eval case miner (PMAT-300).

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
OTHER_MODEL = "nvidia/nemotron-3.5-super-vl:free"
BASE1, HEAD1, OLD1, GONE1 = "a" * 40, "b" * 40, "e" * 40, "f" * 40
BASE2, HEAD2 = "c" * 40, "d" * 40
TODAY = date(2026, 10, 27)


def url(n):
    return f"https://github.com/{REPO}/pull/{n}"


def pr(n, base, head):
    return {"number": n, "title": f"PR {n}", "baseRefOid": base, "headRefOid": head, "url": url(n)}


def report(head_sha, issues, model=MODEL):
    config = SimpleNamespace(base_branch="master", model=model)
    return render_report(overview="Overview.", issues=issues, summaries=[(i.file, "s") for i in issues],
                         config=config, model=model, head_sha=head_sha,
                         file_models={i.file: model for i in issues}, issue_models=[model for _ in issues])


class FakeGh:
    """comparisons: (base, head) -> (status, changed file names), as GitHub's compare API reports them."""

    def __init__(self, prs, comments=None, comparisons=None):
        self.prs, self.comments, self.comparisons, self.calls = prs, comments or {}, comparisons or {}, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["pr", "list"]:
            return json.dumps(self.prs)
        if args[0] == "api" and args[1].endswith("/comments"):  # repos/<owner>/<repo>/issues/<n>/comments
            return "".join(json.dumps(body) + "\n" for body in self.comments.get(int(args[1].split("/")[4]), []))
        if args[0] == "api" and "/compare/" in args[1]:  # repos/<owner>/<repo>/compare/<base>...<head>
            status, files = self.comparisons[tuple(args[1].split("/compare/")[1].split("..."))]
            return json.dumps({"status": status, "files": files, "truncated": False})
        raise AssertionError(f"unexpected gh call: {args}")


def mine(tmp_path, gh, **kw):
    return eval_cases.mine(REPO, limit=10, gh=gh, cases_dir=tmp_path / "cases", today=TODAY, **kw)


def cases(tmp_path):
    return sorted((json.loads(p.read_text()) for p in (tmp_path / "cases").glob("*.json")),
                  key=lambda c: (c["pr_number"], c["finding"]["file"]))


MEDIUM = Issue("src/a.py", 12, "medium", "Index can go out of range.", "Check the length first.")
LOW = Issue("src/b.py", 3, "low", "The name is unclear.", None)


def test_a_finding_on_a_file_unchanged_until_the_merge_is_a_false_positive_case(tmp_path):
    kept = Issue("src/a.py", 7, "high", "Line one.\nLine two.", "Guard it.")
    fixed = Issue("src/b.py", 20, "high", "Changed afterwards.", None)
    final = Issue("src/c.py", 5, "medium", "Raised on the final commit.", None)
    low_kept = Issue("src/a.py", 30, "low", "Low findings are often left by design.", None)  # never a case
    legacy = ("# AI Code Review\n\n> **Models:** per-file=`x` · overview=`x`\n\n## Issues\n\n"
              "### 🔴 High\n\n#### `src/a.py` · line 7\n\n**Problem:** Old format.\n\n---\n")
    gh = FakeGh([pr(1, BASE1, HEAD1)],
                comments={1: [report(OLD1, [kept, fixed, LOW, low_kept], model=OTHER_MODEL), report(HEAD1, [final]),
                              legacy, "LGTM, merging."]},
                comparisons={(OLD1, HEAD1): ("ahead", ["src/b.py"])})

    summary = mine(tmp_path, gh)

    old_case, final_case = cases(tmp_path)
    assert {k: old_case[k] for k in ("repo", "pr_number", "base_sha", "head_sha", "raised_sha", "source", "model")} == {
        "repo": REPO, "pr_number": 1, "base_sha": BASE1, "head_sha": HEAD1, "raised_sha": OLD1,
        "source": "pr_comment", "model": OTHER_MODEL}
    assert old_case["finding"] == {"file": "src/a.py", "line": 7, "severity": "high",
                                   "summary": "Line one.\nLine two.", "fix": "Guard it.", "failure_scenario": None}
    assert (old_case["label"], old_case["label_by"], old_case["label_date"]) == ("false_positive", "rule", "2026-10-27")
    assert (final_case["finding"]["file"], final_case["raised_sha"]) == ("src/c.py", HEAD1)
    assert summary["cases_written"] == 2
    assert summary["skipped"] == {"file_changed": 1, "no_commit": 1, "not_ancestor": 0}


def test_a_commit_rewritten_out_of_the_pr_history_gives_no_cases(tmp_path):
    gh = FakeGh([pr(1, BASE1, HEAD1)], comments={1: [report(GONE1, [MEDIUM])]},
                comparisons={(GONE1, HEAD1): ("diverged", [])})

    summary = mine(tmp_path, gh)

    assert cases(tmp_path) == []
    assert summary["skipped"]["not_ancestor"] == 1


def test_the_same_finding_raised_in_several_reviews_is_one_case(tmp_path):
    again = Issue("src/a.py", 12, "high", "Index may be out of range.", None)  # same line, reworded
    gh = FakeGh([pr(1, BASE1, HEAD1)], comments={1: [report(OLD1, [MEDIUM]), report(HEAD1, [again])]},
                comparisons={(OLD1, HEAD1): ("ahead", [])})

    mine(tmp_path, gh)

    [case] = cases(tmp_path)
    assert case["raised_sha"] == OLD1, "the first review that raised it"
    assert case["times_raised"] == 2


def test_mining_runs_nothing_but_gh_and_mining_again_keeps_a_judges_override(tmp_path, monkeypatch):
    def no_process(*args, **kwargs):
        raise AssertionError(f"mining must run no process (no review, no model): {args}")
    monkeypatch.setattr(eval_cases.subprocess, "run", no_process)
    gh = FakeGh([pr(1, BASE1, HEAD1)], comments={1: [report(HEAD1, [MEDIUM])]})
    mine(tmp_path, gh)
    [case] = cases(tmp_path)
    eval_cases.label(case["case_id"], "real_issue", "The caller never checks the length.",
                     cases_dir=tmp_path / "cases", by="claude-code", today=TODAY)

    mine(tmp_path, gh)

    [case] = cases(tmp_path)
    assert (case["label"], case["label_by"]) == ("real_issue", "claude-code")
    with pytest.raises(ValueError):
        eval_cases.label(case["case_id"], "unlabelled", "x", cases_dir=tmp_path / "cases")


def test_mining_never_writes_to_github(tmp_path):
    gh = FakeGh([pr(1, BASE1, HEAD1), pr(2, BASE2, HEAD2)],
                comments={1: [report(OLD1, [MEDIUM])], 2: [report(HEAD2, [MEDIUM])]},
                comparisons={(OLD1, HEAD1): ("ahead", [])})

    mine(tmp_path, gh)

    assert len(cases(tmp_path)) == 2
    write_flags = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
    for args in gh.calls:
        assert args[:2] == ["pr", "list"] or args[0] == "api", args
        assert not write_flags.intersection(args), args
