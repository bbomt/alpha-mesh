"""Acceptance tests for the hold-out set (PMAT-318). The numbered tests are the ticket's acceptance test."""

import collections
import json
import os
import shutil
import subprocess
import uuid

import jsonschema
import pytest

import baseline
import case_rules
import compare_baseline
import holdout_report
import load_cases
import split_holdout

EVAL = case_rules.EVAL
COMMITTED = json.loads((EVAL / "holdout.json").read_text())
HOLDOUT = set(COMMITTED["cases"])
CASES = case_rules.load_cases()
BASE1_ID_BASED = {"R36-812f904-001", "R36-812f904-002", "R36-812f904-003", "R36-c3e9e57-001",
                  "R36-d2309a5-001", "R36-d2309a5-002", "R37-1fbb920-001", "R37-1fbb920-002"}
SHA = "a" * 40


def in_holdout():
    return [c for c in CASES if c["case_id"] in HOLDOUT]


# 1 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_1_the_holdout_has_both_classes_every_category_and_at_least_10_real_bugs():
    held = in_holdout()
    assert len(held) == len(HOLDOUT), "an ID in holdout.json has no case file"
    assert {c["label"] for c in held} == {"real_issue", "false_positive"}
    assert {c["category"] for c in held} == {c["category"] for c in CASES}
    assert "public_pr" in {c["source"] for c in held}  # public cases share the category `logic`
    assert sum(c["label"] == "real_issue" for c in held) >= 10


# 2 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_2_no_pr_has_cases_on_both_sides():
    sides = collections.defaultdict(set)
    for c in CASES:
        if "pr_number" in c:
            sides[c["repo"], c["pr_number"]].add(c["case_id"] in HOLDOUT)
    assert sides and all(len(s) == 1 for s in sides.values()), {k: v for k, v in sides.items() if len(v) > 1}


def test_2_a_case_with_no_pr_is_its_own_unit():
    synthetic = [c for c in CASES if "pr_number" not in c]
    assert len(synthetic) == 37
    assert {c["case_id"] in HOLDOUT for c in synthetic} == {True, False}


def test_2_no_id_based_case_from_base1_is_in_the_holdout():
    assert not HOLDOUT & BASE1_ID_BASED


def fake(case_id, category="logic", label="real_issue", source="synthetic", **extra):
    return {"case_id": case_id, "repo": "o/r", "category": category, "label": label, "source": source, **extra}


def test_the_draw_keeps_a_pr_together_and_never_groups_cases_without_one():
    cases = [fake(f"s{i}") for i in range(8)] + [fake("a", source="public_pr", pr_number=1),
                                                 fake("b", source="public_pr", pr_number=1)] + [
        fake(f"p{i}", source="public_pr", pr_number=10 + i) for i in range(6)]
    for seed in range(50):
        drawn = split_holdout.draw(cases, seed)
        assert ("a" in drawn) == ("b" in drawn)
        assert sum(c.startswith("s") for c in drawn) == 2  # 25% of 8, not all or none


def test_the_draw_skips_a_pr_too_big_for_the_share_and_leaves_out_cases_that_are_not_blind():
    big = [fake(f"big{i}", "mined", source="pr_comment", pr_number=45) for i in range(22)]
    small = [fake("s1", "mined", source="pr_comment", pr_number=41), fake("s2", "mined", source="pr_comment", pr_number=41)]
    used = [fake(next(iter(sorted(BASE1_ID_BASED))), "mined", source="pr_comment", pr_number=36)]
    for seed in range(50):
        assert split_holdout.draw(big + small + used, seed) == ["s1", "s2"]


def test_the_draw_takes_a_case_whose_result_was_seen_only_when_nothing_else_is_left():
    seen = sorted(split_holdout.RESULT_SEEN)
    security = [fake("sec-001", "security"), *[fake(f"sec-x{i}", "security") for i in range(3)]]
    assert all("sec-001" not in split_holdout.draw(security, seed) for seed in range(50))
    degenerate = [fake(c, "degenerate") for c in seen if c.startswith("deg")]
    assert len(split_holdout.draw(degenerate, 1)) == 1  # every degenerate case was seen, and the category must be there


# 3 ──────────────────────────────────────────────────────────────────────────────────────────────

FAKE_PROMPTFOO = """#!/usr/bin/env bash
exec "$PROMPTFOO_PYTHON" "$FAKE_PROMPTFOO_PY" "$@"
"""
# Stands in for Promptfoo: loads the tests the way the config does, and writes an empty review for each.
# So every raise_true_bug case fails. Exits as Promptfoo does when a test fails.
FAKE_PROMPTFOO_PY = """
import json, os, sys
sys.path.insert(0, "promptfoo")
import load_cases
tests = load_cases.create_tests()
review = {"head_sha": "x", "model": "m", "files": [], "incomplete": [], "excluded": [], "findings": []}
rows = [{"vars": {"case_id": t["vars"]["case_id"]}, "repeatIndex": 0, "failureReason": 0, "response": {"output": review}}
        for t in tests]
out = sys.argv[sys.argv.index("-o") + 1]
json.dump({"results": {"results": rows}}, open(out, "w"))
json.dump({"args": sys.argv[1:], "cases": [t["vars"]["case_id"] for t in tests]}, open(os.environ["FAKE_RECORD"], "w"))
sys.exit(100)
"""


@pytest.fixture
def run_eval(tmp_path):
    """Runs the real run_eval.sh with a fake promptfoo and nvm; removes its run folder afterwards."""
    name = f"test-{uuid.uuid4().hex}"
    (tmp_path / "bin").mkdir()
    fake_bin = tmp_path / "bin" / "promptfoo"
    fake_bin.write_text(FAKE_PROMPTFOO)
    fake_bin.chmod(0o755)
    (tmp_path / "fake.py").write_text(FAKE_PROMPTFOO_PY)
    (tmp_path / "nvm.sh").write_text("nvm() { :; }\n")
    env = os.environ | {"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "NVM_DIR": str(tmp_path),
                        "FAKE_PROMPTFOO_PY": str(tmp_path / "fake.py"), "FAKE_RECORD": str(tmp_path / "record.json")}
    env.pop("EVAL_HOLDOUT", None)

    def call(*flags):
        done = subprocess.run([str(EVAL / "scripts" / "run_eval.sh"), "--model", "m", "--k", "1", "--run", name, *flags],
                              env=env, capture_output=True, text=True)
        return done, json.loads((tmp_path / "record.json").read_text()), EVAL / "runs" / name
    yield call
    shutil.rmtree(EVAL / "runs" / name, ignore_errors=True)


def test_3_a_default_run_leaves_the_holdout_out(run_eval):
    done, record, run_dir = run_eval()

    assert done.returncode == 100, done.stderr
    assert set(record["cases"]) == {c["case_id"] for c in CASES} - HOLDOUT
    assert (run_dir / "results.json").exists()
    assert "Hold-out" not in done.stdout


def test_3_the_flag_runs_only_the_holdout_and_reports_its_pass_rate_apart(run_eval):
    done, record, run_dir = run_eval("--holdout")

    assert set(record["cases"]) == HOLDOUT
    assert "--no-table" in record["args"]  # Promptfoo's table would show the failures
    assert not (run_dir / "results.json").exists()  # baseline.py and compare_baseline.py read that file
    assert (run_dir / "results-holdout.json").exists()
    real = sum(c["label"] == "real_issue" for c in in_holdout())
    assert f"Hold-out: {len(HOLDOUT)} cases" in done.stdout
    assert f"true bugs raised 0/{real} runs" in done.stdout  # the fake's empty review misses each of them
    assert not [c for c in HOLDOUT if c in done.stdout]  # failing cases stay unread
    assert done.returncode == 100, done.stderr  # failing tests still fail the script, as without the flag


def test_the_loader_returns_development_cases_unless_the_holdout_is_asked_for(monkeypatch):
    monkeypatch.delenv("EVAL_HOLDOUT", raising=False)
    assert not {t["vars"]["case_id"] for t in load_cases.create_tests()} & HOLDOUT
    monkeypatch.setenv("EVAL_HOLDOUT", "1")
    assert {t["vars"]["case_id"] for t in load_cases.create_tests()} == HOLDOUT


def test_the_holdout_report_scores_only_the_holdout_from_a_run_of_every_case():
    empty = {"head_sha": "x", "model": "m", "files": [], "incomplete": [], "excluded": [], "findings": []}
    rows = [{"vars": {"case_id": c["case_id"]}, "repeatIndex": 0, "failureReason": 0, "response": {"output": empty}}
            for c in CASES]

    report = holdout_report.report({"results": {"results": rows}}, in_holdout())

    assert report.startswith(f"Hold-out: {len(HOLDOUT)} cases")
    assert not [c["case_id"] for c in CASES if c["case_id"] in report]


# 4 ──────────────────────────────────────────────────────────────────────────────────────────────

def case_file(case_id):
    return {"case_id": case_id, "repo": "org/repo", "pr_number": 1, "base_sha": "b" * 40, "raised_sha": SHA,
            "finding": {"file": "src/a.py", "line": 10, "severity": "medium"}, "label": "real_issue",
            "label_reason": "Fixed.", "category": "mined", "notes": "",
            "expected": {"action": "raise_true_bug", "flag_file": "src/a.py", "min_severity": "medium"}}


@pytest.fixture
def small_repo(tmp_path, monkeypatch):
    """A git repo with one development case and one hold-out case, and a run where both fail."""
    root = tmp_path / "repo"
    (root / "cases" / "repo").mkdir(parents=True)
    for case_id in ("DEV", "HELD"):
        (root / "cases" / "repo" / f"{case_id}.json").write_text(json.dumps(case_file(case_id)))
    (root / "holdout.json").write_text(json.dumps({"seed": 1, "cases": ["HELD"]}))

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "cases")
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"commit": git("rev-parse", "HEAD"), "model": "m", "date": "2026-11-05",
                                              "k": 1, "window": 3, "dirty": False}))
    empty = {"head_sha": SHA, "model": "m", "files": [], "incomplete": [], "excluded": [], "findings": []}
    (run / "results.json").write_text(json.dumps({"results": {"results": [
        {"vars": {"case_id": c}, "repeatIndex": 0, "failureReason": 0, "response": {"output": empty}}
        for c in ("DEV", "HELD")]}}))
    monkeypatch.setattr(case_rules, "CASES_DIR", root / "cases")
    monkeypatch.setattr(case_rules, "HOLDOUT", root / "holdout.json")
    monkeypatch.setattr(baseline, "REPO_ROOT", root)
    monkeypatch.setattr(baseline, "BASELINE", root / "baseline.json")
    monkeypatch.setattr(baseline, "ANALYSIS", root / "error_analysis.json")
    return root, run


def test_4_a_baseline_builds_with_a_failing_holdout_case_that_has_no_error_analysis_entry(small_repo):
    root, run = small_repo
    (root / "error_analysis.json").write_text(json.dumps(
        {"DEV": {"failure_class": "model_failure", "reason": "Misses the bug at line 10."}}))

    assert baseline.main([str(run)]) == 0

    assert set(json.loads((root / "baseline.json").read_text())["cases"]) == {"DEV"}


def test_a_failing_development_case_still_needs_an_entry_and_the_refusal_never_names_a_holdout_case(small_repo, capsys):
    _, run = small_repo

    assert baseline.main([str(run)]) == 1

    err = capsys.readouterr().err
    assert "DEV" in err and "HELD" not in err


def test_a_label_change_on_a_holdout_case_stays_out_of_the_baseline(small_repo):
    root, run = small_repo
    (root / "error_analysis.json").write_text(json.dumps(
        {"DEV": {"failure_class": "model_failure", "reason": "Misses the bug at line 10."}}))
    moved = case_file("HELD")
    moved["finding"]["line"] = 40
    (root / "cases" / "repo" / "HELD.json").write_text(json.dumps(moved))
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "moved"], check=True)

    assert baseline.main([str(run)]) == 0

    assert json.loads((root / "baseline.json").read_text())["label_errors_fixed"] == []


def test_compare_never_reports_a_holdout_case(small_repo):
    root, run = small_repo
    old = {"cases_commit": json.loads((run / "run.json").read_text())["commit"], "cases": {}}

    report = compare_baseline.compare(old, json.loads((run / "results.json").read_text()),
                                      cases_dir=root / "cases", repo_root=root)

    assert report["new_failures"] == ["DEV"]


# 5 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_5_rerunning_the_draw_with_the_committed_seed_gives_the_committed_list():
    """Adding a case changes the draw, so this fails on purpose: a new case goes to development and the list stays,
    or the set is redrawn on purpose (eval/README.md)."""
    assert split_holdout.draw(CASES, COMMITTED["seed"]) == COMMITTED["cases"]


# 6 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_6_the_committed_baseline_holds_development_cases_only_every_failing_one_classed():
    committed = json.loads((EVAL / "baseline.json").read_text())
    jsonschema.validate(committed, baseline.SCHEMA)
    assert set(committed["cases"]) == {c["case_id"] for c in CASES} - HOLDOUT
    failing = {k: v for k, v in committed["cases"].items() if v["status"] != "pass"}
    assert failing and all(v["failure_class"] and v["reason"] for v in failing.values())


def test_6_the_error_analysis_has_no_entry_for_a_holdout_case():
    analysis = json.loads((EVAL / "error_analysis.json").read_text())
    assert not set(analysis) & HOLDOUT
    assert set(analysis) <= {c["case_id"] for c in CASES}  # none left over from a deleted case
