"""Acceptance tests for the baseline and its comparison (PMAT-304 (c), (d)).

Results files are in Promptfoo's `-o results.json` format, with outputs as the provider returns them.
Label changes are read from a throwaway git repository.
"""

import json
import subprocess

import jsonschema
import pytest

import write_baseline as baseline
import case_rules
import compare_baseline

SHA = "a" * 40


@pytest.fixture(autouse=True)
def _never_the_real_baseline(tmp_path, monkeypatch):
    """baseline.main writes eval/baseline.json: tests write a temp copy instead."""
    monkeypatch.setattr(baseline, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(baseline, "ANALYSIS", tmp_path / "error_analysis.json")


def case(case_id, action="skip_false_bug", line=10, label="false_positive", label_reason="Dismissed."):
    expected = {"action": action, "flag_file": "src/a.py"} | ({"min_severity": "medium"} if action == "raise_true_bug" else {})
    return {"case_id": case_id, "repo": "org/repo", "pr_number": 1, "base_sha": "b" * 40, "raised_sha": SHA,
            "finding": {"file": "src/a.py", "line": line, "severity": "medium"}, "label": label,
            "label_reason": label_reason, "category": "logic", "expected": expected, "notes": ""}


def review(*lines):
    return {"head_sha": SHA, "model": "m", "files": ["src/a.py"], "incomplete": [], "excluded": [],
            "findings": [{"file": "src/a.py", "line": n, "severity": "medium", "summary": "s", "suggestion": None}
                         for n in lines]}


def row(case_id, repeat, output=None, error=None):
    """One Promptfoo result row."""
    return {"vars": {"case_id": case_id}, "repeatIndex": repeat, "failureReason": 2 if error else 0,
            "error": error, "response": {"output": output} if output is not None else {"error": error}}


def results(*rows):
    return {"results": {"results": list(rows)}}


JUDGE = "judge/model"


def judged(r, correctness=3, coverage=4, actionability=5, error=None, model=JUDGE, rubric="v1"):
    """The row with the llm_rubric assert's result, as llm_rubric.judge_results returns it: a judgment, or a judge
    error (never a score)."""
    meta = {"judge_error": error} if error else {"judgment": {
        "correctness": {"score": correctness}, "coverage": {"score": coverage},
        "actionability": {"score": actionability}, "rubric": rubric, "model_requested": model}}
    return r | {"gradingResult": {"componentResults": [
        {"assertion": {"value": "file://promptfoo/llm_rubric.py:judge_results"}, "metadata": meta}]}}


def judge_block(correctness=3.0, coverage=4.0, actionability=5.0, runs=6, errors=0):
    return {"model": JUDGE, "rubric": "v1", "runs": runs, "judged": runs - errors, "judge_errors": errors,
            "not_judged": 0, "mean": {"correctness": correctness, "coverage": coverage, "actionability": actionability}}


# (c) ────────────────────────────────────────────────────────────────────────────────────────────

def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def write(root, c):
    path = root / "cases" / "repo" / f"{c['case_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c, indent=2) + "\n")


def test_compare_reports_new_failures_new_passes_and_label_changes(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    for c in (case("A"), case("B"), case("C")):
        write(root, c)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "cases")
    base_commit = git(root, "rev-parse", "HEAD")
    # B was dismissed, but the review was right: relabelled with evidence after the baseline.
    write(root, case("B", "raise_true_bug", label="real_issue",
                     label_reason="Dismissed as unreachable, but src/a.py:40 calls it when --force is set."))
    git(root, "commit", "-qam", "fix[PMAT-1]: fixed label error in eval case B (reachable via --force)")

    old = {"cases_commit": base_commit, "cases": {"A": {"status": "pass"}, "B": {"status": "stable_failure"},
                                            "C": {"status": "pass"}}}
    new = results(row("A", 0, review(10)),  # A re-raised its false bug at line 10: now fails
                  row("B", 0, review(11)),  # B is now a true bug at line 10, and is raised: now passes
                  row("C", 0, review()))

    report = compare_baseline.compare(old, new, cases_dir=root / "cases", repo_root=root)

    assert report["new_failures"] == ["A"]
    assert report["new_passes"] == ["B"]
    assert report["unchanged"] == ["C"]
    assert [(c["case_id"], c["old_label"], c["new_label"]) for c in report["label_changes"]] == [
        ("B", "false_positive", "real_issue")]
    assert "src/a.py:40" in report["label_changes"][0]["reason"]


def test_moving_a_case_line_is_a_label_change(tmp_path):
    """Matching is by location, so a corrected finding.line changes what the case expects."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    write(root, case("A", line=10))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "cases")
    base_commit = git(root, "rev-parse", "HEAD")
    write(root, case("A", line=23) | {"notes": "the code the finding describes is at line 23 at raised_sha"})

    changes = baseline.label_changes(base_commit, root / "cases", root)

    assert [(c["case_id"], c["old_line"], c["new_line"]) for c in changes] == [("A", 10, 23)]
    assert "line 23" in changes[0]["reason"]


# (d)────────────────────────────────────────────────────────────────────────────────────────────

def valid_baseline():
    return {"run": "base1", "commit": SHA, "cases_commit": SHA, "model": "m", "date": "2026-10-30", "k": 2, "window": 3,
            "summary": {"cases": 2, "scored_cases": 2, "pass_rate": 0.75, "pass_k": 0.5, "file_level_pass_rate": 0.5,
                        "by_class": {"model_failure": 1, "label_error": 1, "harness_error": 0}},
            "label_errors_fixed": [{"case_id": "B", "reason": "src/a.py:40 calls it when --force is set."}],
            "judge": judge_block(),
            "cases": {"A": {"runs": ["pass", "pass"], "file_level_runs": ["pass", "fail"], "status": "pass",
                            "failure_class": None, "reason": None, "category": "logic", "action": "skip_false_bug"},
                      "B": {"runs": ["pass", "fail"], "file_level_runs": ["fail", "fail"], "status": "flaky",
                            "failure_class": "model_failure", "reason": "Re-raises the dismissed claim at line 12.",
                            "category": "logic", "action": "skip_false_bug"}}}


def test_a_valid_baseline_passes_validation():
    jsonschema.validate(valid_baseline(), baseline.SCHEMA)


def test_the_committed_baseline_passes_its_guard():
    """eval/baseline.json is what compare_baseline.py compares against: it must stay valid and analysed."""
    committed = json.loads((case_rules.EVAL / "baseline.json").read_text())
    jsonschema.validate(committed, baseline.SCHEMA)


def test_the_committed_baseline_covers_exactly_the_current_development_cases():
    committed = json.loads((case_rules.EVAL / "baseline.json").read_text())
    assert set(committed["cases"]) == {c["case_id"] for c in case_rules.development_cases()}


@pytest.mark.parametrize("broken", [
    lambda b: b["label_errors_fixed"][0].pop("reason"),
    lambda b: b["label_errors_fixed"][0].update(reason=""),
    lambda b: b["label_errors_fixed"][0].pop("case_id"),
    lambda b: b["cases"]["B"].update(failure_class=None),
    lambda b: b["cases"]["B"].update(reason=""),
    lambda b: b["cases"]["B"].update(failure_class="measurement_error"),
    lambda b: b["cases"]["B"].update(reason=7),
    lambda b: b["cases"]["A"].update(file_level_runs=["maybe"]),
    lambda b: b.pop("judge"),
    lambda b: b["judge"].update(not_judged=2),
    lambda b: b["judge"]["mean"].update(correctness=7),
], ids=["label error without a reason", "label error with an empty reason", "label error without a case ID",
        "failing case without a class", "failing case without a reason", "unknown class",
        "reason not text", "unknown file-level result", "no judge means", "repeats not judged",
        "judge mean out of range"])
def test_the_baseline_guard_rejects_an_unjustified_entry(broken):
    bad = valid_baseline()
    broken(bad)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, baseline.SCHEMA)


# Building the baseline ───────────────────────────────────────────────────────────────────────────

def build(analysis=None, label_changes=()):
    cases = [case("A"), case("B"), case("E", "raise_true_bug", label="real_issue")]
    rows = [row("A", 0, review()), row("A", 1, review()),
            row("B", 0, review()), row("B", 1, review(12)),
            row("E", 0, error="ReviewFailed: the review left no result"), row("E", 1, review(10))]
    meta = {"run": "base1", "commit": SHA, "cases_commit": SHA, "model": "m", "date": "2026-10-30", "k": 2, "window": 3}
    return baseline.build(results(*rows), cases, meta, analysis=analysis or {}, label_changes=list(label_changes),
                          judge=judge_block())


def test_the_baseline_records_runs_stability_and_classes():
    b = build({"B": {"failure_class": "model_failure", "reason": "Re-raises the dismissed claim at line 12."}},
              [{"case_id": "B", "reason": "src/a.py:40 calls it when --force is set."}])

    jsonschema.validate(b, baseline.SCHEMA)
    assert b["cases"]["A"]["status"] == "pass"
    assert b["cases"]["B"]["runs"] == ["pass", "fail"] and b["cases"]["B"]["status"] == "flaky"
    assert b["cases"]["E"]["status"] == "harness_error" and b["cases"]["E"]["failure_class"] == "harness_error"
    assert "left no result" in b["cases"]["E"]["reason"]
    assert (b["summary"]["scored_cases"], b["summary"]["pass_rate"], b["summary"]["pass_k"]) == (2, 0.75, 0.5)
    assert b["summary"]["by_class"] == {"model_failure": 1, "label_error": 1, "harness_error": 1}
    assert b["label_errors_fixed"] == [{"case_id": "B", "reason": "src/a.py:40 calls it when --force is set."}]


def test_the_baseline_records_pass_rates_per_category_and_severity():
    """Public cases share the category `logic` with synthetic ones, so they get their own row (README)."""
    high = case("P", "raise_true_bug", label="real_issue") | {"source": "public_pr", "category": "logic"}
    high["finding"] = {**high["finding"], "severity": "high"}
    clean = {k: v for k, v in case("N").items() if k != "finding"} | {"category": "style_only"}
    clean["expected"] = {"action": "raise_nothing", "flag_file": "src/*.py"}
    rows = [row("P", 0, review(10)), row("P", 1, review()), row("N", 0, review()), row("N", 1, review())]
    meta = {"commit": SHA, "cases_commit": SHA, "model": "m", "date": "2026-11-05", "k": 2, "window": 3}
    analysis = {"P": {"failure_class": "model_failure", "reason": "Misses the bug in repeat 1."}}

    s = baseline.build(results(*rows), [high, clean], meta, analysis=analysis, label_changes=[],
                       judge=judge_block())["summary"]

    assert s["by_category"] == {
        "public/raise_true_bug": {"cases": 1, "runs_passed": 1, "runs": 2, "passed_all": 0},
        "style_only/raise_nothing": {"cases": 1, "runs_passed": 2, "runs": 2, "passed_all": 1}}
    assert s["by_severity"] == {
        "high/raise_true_bug": {"cases": 1, "runs_passed": 1, "runs": 2, "passed_all": 0},
        "none/raise_nothing": {"cases": 1, "runs_passed": 2, "runs": 2, "passed_all": 1}}


def test_the_baseline_is_refused_until_every_failing_case_has_been_analysed():
    with pytest.raises(baseline.NotAnalysed, match="B"):
        build()


def test_the_file_level_rule_is_kept_for_comparison():
    c = case("A", line=30)
    assert case_rules.check(c, review(10))[0] is True       # another finding elsewhere in the file
    assert case_rules.check_file_level(c, review(10)) is False  # the old rule (PMAT-309) failed it


def test_label_changes_fail_loudly_on_an_unknown_commit(tmp_path):
    root = tmp_path / "repo"
    (root / "cases").mkdir(parents=True)
    git(root, "init", "-q")
    with pytest.raises(RuntimeError, match="git diff"):
        baseline.label_changes("0" * 40, root / "cases", root)


def test_a_commit_that_looks_like_a_git_option_writes_nothing(tmp_path):
    """cases_commit comes from a baseline file; `git diff --output=<file>` would overwrite that file."""
    root = tmp_path / "repo"
    (root / "cases").mkdir(parents=True)
    git(root, "init", "-q")
    target = tmp_path / "written-by-git"
    with pytest.raises(RuntimeError, match="git diff"):
        baseline.label_changes(f"--output={target}", root / "cases", root)
    assert not target.exists()


def test_a_run_made_with_uncommitted_changes_never_becomes_the_baseline(tmp_path, capsys):
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"commit": SHA, "model": "m", "date": "2026-10-30", "k": 1,
                                              "window": 3, "dirty": True}))
    (run / "results.json").write_text(json.dumps(results()))

    assert baseline.main([str(run)]) == 1
    assert "uncommitted" in capsys.readouterr().err


def test_a_failing_git_stops_the_baseline_instead_of_reading_as_clean(tmp_path, monkeypatch):
    """git status failing (not a repository) must not pass the uncommitted-cases guard."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"commit": SHA, "model": "m", "date": "2026-10-30", "k": 1,
                                              "window": 3, "dirty": False}))
    (run / "results.json").write_text(json.dumps(results()))
    monkeypatch.setattr(baseline, "REPO_ROOT", tmp_path / "not-a-repo")

    with pytest.raises(RuntimeError, match="git status"):
        baseline.main([str(run)])


# The judge's means (PMAT-340) ──────────────────────────────────────────────────────────────────────────

def test_the_baseline_records_the_judges_means_over_every_repeat_and_a_judge_error_is_not_a_score():
    cases = [case("A"), case("B")]
    rows = [judged(row("A", 0, review()), 2, 4, 5), judged(row("A", 1, review()), 4, 2, 3),
            judged(row("B", 0, review()), error="the judge's reply had no score"), judged(row("B", 1, review()), 3, 3, 4)]

    out = baseline.judge_means(results(*rows), cases)

    assert out == {"model": JUDGE, "rubric": "v1", "runs": 4, "judged": 3, "judge_errors": 1, "not_judged": 0,
                   "mean": {"correctness": 3.0, "coverage": 3.0, "actionability": 4.0}}


def test_the_baseline_is_refused_when_a_repeat_was_not_judged():
    """A run with --judge-repeats first (or none) leaves rows with no judgment: their means miss part of the run."""
    rows = [judged(row("A", 0, review())), row("A", 1, review())]
    with pytest.raises(baseline.NotJudged, match="1 of 2 runs were not judged"):
        baseline.judge_means(results(*rows), [case("A")])


def write_run(tmp_path, res):
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"commit": SHA, "model": "m", "date": "2026-11-18", "k": 2,
                                              "window": 3, "dirty": False}))
    (run / "results.json").write_text(json.dumps(res))
    return run


@pytest.fixture
def clean_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(baseline, "label_changes", lambda *a: [])
    monkeypatch.setattr(case_rules, "development_cases", lambda: [case("A")])
    monkeypatch.setattr(baseline.subprocess, "run",
                        lambda *a, **k: type("Done", (), {"returncode": 0, "stdout": SHA if "rev-parse" in a[0] else "",
                                                          "stderr": ""})())


def test_judge_results_from_a_stored_run_of_the_same_reviews_fill_the_baseline(tmp_path, clean_repo):
    plain = [row("A", 0, review()), row("A", 1, review())]
    run = write_run(tmp_path, results(*plain))
    judge_file = tmp_path / "judge-results.json"
    replayed = [r | {"response": {"output": r["response"]["output"] | {"repeat": r["repeatIndex"]}}} for r in plain]
    judge_file.write_text(json.dumps(results(*(judged(r, 2, 4, 5) for r in replayed))))  # --stored adds `repeat`

    assert baseline.main([str(run)]) == 1  # the run's own rows hold no judgment
    assert baseline.main([str(run), "--judge-results", str(judge_file)]) == 0

    written = json.loads(baseline.BASELINE.read_text())
    assert written["judge"]["mean"] == {"correctness": 2.0, "coverage": 4.0, "actionability": 5.0}
    assert written["judge"]["run"] == tmp_path.name  # the folder holding the judge results


def test_judge_results_of_other_reviews_are_refused(tmp_path, clean_repo, capsys):
    run = write_run(tmp_path, results(row("A", 0, review()), row("A", 1, review())))
    judge_file = tmp_path / "judge-results.json"
    judge_file.write_text(json.dumps(results(judged(row("A", 0, review())), judged(row("A", 1, review(10))))))

    assert baseline.main([str(run), "--judge-results", str(judge_file)]) == 1
    assert "other reviews" in capsys.readouterr().err and not baseline.BASELINE.exists()
