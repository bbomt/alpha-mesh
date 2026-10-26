"""Acceptance tests for the eval case miner (PMAT-300).

The canned reports are made by the review's own render_report, so the parser is tested
against the exact text `mesh review-pull-request` prints and posts. gh and mesh are fakes
that record every call: nothing reaches GitHub or a model.
"""

import json
from datetime import date
from types import SimpleNamespace

import eval_cases
from review_pull_request.formatter import render_report
from review_pull_request.stage2 import Issue

REPO = "org/repo"
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
OTHER_MODEL = "nvidia/nemotron-3.5-super-vl:free"
BASE1, HEAD1, OLD1 = "a" * 40, "b" * 40, "e" * 40
BASE2, HEAD2 = "c" * 40, "d" * 40
TODAY = date(2026, 10, 26)


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
    def __init__(self, prs, comments=None):
        self.prs, self.comments, self.calls = prs, comments or {}, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["pr", "list"]:
            return json.dumps(self.prs)
        if args[0] == "api":  # repos/<owner>/<repo>/issues/<n>/comments
            return "".join(json.dumps(body) + "\n" for body in self.comments.get(int(args[1].split("/")[4]), []))
        raise AssertionError(f"unexpected gh call: {args}")


class FakeMesh:
    """The mesh wrapper merges stderr into stdout, so progress lines surround the report."""

    def __init__(self, reports):
        self.reports, self.calls = reports, []

    def __call__(self, args):
        self.calls.append(list(args))
        return f"Model: x\nFetching diff...\n{self.reports[args[0]]}\nPR review complete! Reviewed 2 file(s) in 0m 3s.\n"


def mine(tmp_path, gh, mesh, **kw):
    return eval_cases.mine(REPO, limit=10, model=MODEL, gh=gh, mesh=mesh, cases_dir=tmp_path / "cases",
                           reports_dir=tmp_path / "reports", today=TODAY, **kw)


def cases(tmp_path):
    return sorted((json.loads(p.read_text()) for p in (tmp_path / "cases").glob("*.json")),
                  key=lambda c: (c["pr_number"], c["source"]))


MEDIUM = Issue("src/a.py", 12, "medium", "Index can go out of range.", "Check the length first.")
LOW = Issue("src/b.py", 3, "low", "The name is unclear.", None)


def test_run_scores_prs_and_writes_one_unlabelled_case(tmp_path):
    gh = FakeGh([pr(1, BASE1, HEAD1), pr(2, BASE2, HEAD2)])
    mesh = FakeMesh({url(1): report(HEAD1, [MEDIUM, LOW]), url(2): report(HEAD2, [LOW])})

    mine(tmp_path, gh, mesh)

    rep = json.loads((tmp_path / "reports" / "eval_report_20261026.json").read_text())
    assert rep["model"] == MODEL and rep["run_date"] == "2026-10-26" and rep["repo"] == REPO
    assert [(p["pr_number"], p["passed"], p["false_positives"]) for p in rep["prs"]] == [(1, False, 1), (2, True, 0)]
    assert rep["totals"]["prs_tested"] == 2
    assert rep["totals"]["false_positive_rate"] == 0.5
    [case] = cases(tmp_path)
    assert {k: case[k] for k in ("repo", "pr_number", "base_sha", "head_sha", "source", "model", "label")} == {
        "repo": REPO, "pr_number": 1, "base_sha": BASE1, "head_sha": HEAD1, "source": "fresh_run",
        "model": MODEL, "label": "unlabelled"}
    assert case["finding"] == {"file": "src/a.py", "line": 12, "severity": "medium",
                               "summary": "Index can go out of range.", "fix": "Check the length first.",
                               "failure_scenario": None}
    assert case["label_by"] is case["label_date"] is case["label_reason"] is None
    assert case["rerun_model"] is case["rerun_reproduced"] is None
    assert rep["prs"][0]["case_ids"] == [case["case_id"]]


def test_mining_again_keeps_existing_labels(tmp_path):
    gh = FakeGh([pr(1, BASE1, HEAD1)])
    mesh = FakeMesh({url(1): report(HEAD1, [MEDIUM])})
    mine(tmp_path, gh, mesh)
    [case] = cases(tmp_path)
    eval_cases.label(case["case_id"], "false_positive", "The length is checked by the caller.",
                     cases_dir=tmp_path / "cases", by="claude-code", today=TODAY)

    mine(tmp_path, gh, mesh)

    [case] = cases(tmp_path)
    assert case["label"] == "false_positive"
    assert (case["label_by"], case["label_date"]) == ("claude-code", "2026-10-26")


def test_an_eval_run_never_posts_to_github(tmp_path):
    gh = FakeGh([pr(1, BASE1, HEAD1)], comments={1: [report(OLD1, [MEDIUM])]})
    mesh = FakeMesh({url(1): report(HEAD1, [MEDIUM])})

    mine(tmp_path, gh, mesh)
    eval_cases.rerun(cases_dir=tmp_path / "cases", mesh=mesh, today=TODAY)

    assert mesh.calls, "the review was never run"
    for args in mesh.calls:
        assert "--no-post-comment" in args and "--dry-run" not in args
    write_flags = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
    for args in gh.calls:
        assert args[:2] == ["pr", "list"] or args[0] == "api", args
        assert not write_flags.intersection(args), args


def test_a_case_whose_rerun_does_not_reproduce_is_left_out_of_the_rates(tmp_path):
    other = Issue("src/c.py", 40, "medium", "Lock is never released.", None)
    gh = FakeGh([pr(1, BASE1, HEAD1), pr(2, BASE2, HEAD2)])
    mesh = FakeMesh({url(1): report(HEAD1, [MEDIUM]), url(2): report(HEAD2, [other])})
    rep = mine(tmp_path, gh, mesh)
    assert rep["labelled"]["false_positive_rate"] == 0.0  # nothing judged yet
    pr1_case, pr2_case = cases(tmp_path)
    eval_cases.label(pr2_case["case_id"], "false_positive", "The context manager releases it.",
                     cases_dir=tmp_path / "cases", by="claude-code", today=TODAY)
    moved = Issue("src/a.py", 30, "medium", "Index can go out of range.", None)  # 18 lines away
    rerun_mesh = FakeMesh({url(1): report(HEAD1, [moved], model=MODEL)})

    eval_cases.rerun(cases_dir=tmp_path / "cases", mesh=rerun_mesh, today=TODAY)

    assert [c[0] for c in rerun_mesh.calls] == [url(1)], "a labelled case is not re-run"
    assert ["--model", MODEL] == rerun_mesh.calls[0][-2:]
    pr1_case, pr2_case = cases(tmp_path)
    assert pr1_case["label"] == "not_reproduced" and pr1_case["rerun_reproduced"] is False
    assert (pr1_case["rerun_model"], pr1_case["rerun_sha"]) == (MODEL, HEAD1)
    labelled = eval_cases.report(tmp_path / "reports" / "eval_report_20261026.json",
                                 cases_dir=tmp_path / "cases")["labelled"]
    assert labelled["prs_with_false_positive"] == [2]
    assert labelled["false_positive_rate"] == 0.5
    assert labelled["label_counts"] == {"unlabelled": 0, "false_positive": 1, "real_issue": 0, "not_reproduced": 1}


def test_a_rerun_finding_near_the_same_line_reproduces_the_case(tmp_path):
    gh = FakeGh([pr(1, BASE1, HEAD1)])
    mine(tmp_path, gh, FakeMesh({url(1): report(HEAD1, [MEDIUM])}))
    near = Issue("src/a.py", 16, "high", "Out-of-range index.", None)  # 4 lines away

    eval_cases.rerun(cases_dir=tmp_path / "cases", mesh=FakeMesh({url(1): report(HEAD1, [near])}), today=TODAY)

    [case] = cases(tmp_path)
    assert case["label"] == "unlabelled" and case["rerun_reproduced"] is True
    assert case["rerun_finding"]["line"] == 16 and case["rerun_finding"]["severity"] == "high"


def test_review_comments_on_the_pr_become_cases_at_the_commit_they_reviewed(tmp_path):
    high = Issue("src/a.py", 7, "high", "Line one.\nLine two.", "Guard it.")
    legacy = ("# AI Code Review\n\n> **Models:** per-file=`x` · overview=`x`\n\n## Issues\n\n"
              "### 🔴 High\n\n#### `src/a.py` · line 7\n\n**Problem:** Old format.\n\n---\n")
    gh = FakeGh([pr(1, BASE1, HEAD1)], comments={1: [
        report(OLD1, [high, LOW], model=OTHER_MODEL), legacy, "LGTM, merging."]})
    mesh = FakeMesh({url(1): report(HEAD1, [])})

    rep = mine(tmp_path, gh, mesh)

    [case] = cases(tmp_path)
    assert {k: case[k] for k in ("pr_number", "base_sha", "head_sha", "source", "model")} == {
        "pr_number": 1, "base_sha": BASE1, "head_sha": OLD1, "source": "pr_comment", "model": OTHER_MODEL}
    assert case["finding"]["severity"] == "high" and case["finding"]["line"] == 7
    assert case["finding"]["summary"] == "Line one.\nLine two."
    assert rep["comments_skipped_no_commit"] == 1
    assert rep["prs"][0]["passed"] is True, "only the fresh run is scored"

    eval_cases.rerun(cases_dir=tmp_path / "cases", mesh=FakeMesh({url(1): report(HEAD1, [])}), today=TODAY)

    [case] = cases(tmp_path)
    assert case["label"] == "not_reproduced" and case["rerun_sha"] == HEAD1
    assert OLD1[:7] in case["label_reason"] and "fixed before the merge" in case["label_reason"]
