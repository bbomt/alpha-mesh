"""Every case file matches eval/schema/case.schema.json, and no two cases contradict (PMAT-309)."""

import json
from pathlib import Path

import jsonschema
import pytest

import eval_cases
from case_rules import glob_regex

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schema" / "case.schema.json").read_text())
CASES = eval_cases.case_files(eval_cases.CASES_DIR)


def contradictions(cases: list[dict]) -> list[tuple[str, str]]:
    """(raise_nothing case, other case) pairs at the same repo and commit whose files overlap:
    a file cannot be both clean and hold a finding."""
    found = []
    for clean in cases:
        if clean["expected"]["action"] != "raise_nothing":
            continue
        pattern = glob_regex(clean["expected"]["flag_file"])
        for other in cases:
            if (other["expected"]["action"] != "raise_nothing"  # two clean cases agree
                    and (other["repo"], other.get("raised_sha")) == (clean["repo"], clean.get("raised_sha"))
                    and pattern.match(other["expected"]["flag_file"])):
                found.append((clean["case_id"], other["case_id"]))
    return found


def case(action, flag_file, **expected):
    c = {"case_id": f"{action}-{flag_file}", "repo": "org/repo", "pr_number": 1, "raised_sha": "a" * 40,
         "label": "real_issue", "category": "logic", "notes": "",
         "expected": {"action": action, "flag_file": flag_file, **expected}}
    if action != "raise_nothing":
        c["finding"] = {"file": flag_file, "line": 10}
    return c


def test_the_dataset_is_not_empty():
    assert CASES


@pytest.mark.parametrize("path", CASES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_case_file_matches_the_schema(path):
    jsonschema.validate(json.loads(path.read_text()), SCHEMA)


def test_no_two_cases_in_the_dataset_contradict():
    assert contradictions(eval_cases.load_cases(eval_cases.CASES_DIR)) == []


@pytest.mark.parametrize("broken", [
    lambda c: c.pop("expected"),
    lambda c: c.pop("category"),
    lambda c: c.pop("notes"),
    lambda c: c["expected"].pop("action"),
    lambda c: c["expected"].pop("flag_file"),
    lambda c: c["expected"].update(action="must_flag"),
    lambda c: c["expected"].update(max_comments=0),  # removed key
    lambda c: c["expected"].update(min_severity="critical"),
    lambda c: c["expected"].pop("min_severity"),
    lambda c: c["expected"].update(flag_file="src/*.py"),
    lambda c: c.update(expected={"action": "skip_false_bug", "flag_file": "src/**/a.py"}),
    lambda c: c.update(expected={"action": "skip_false_bug", "flag_file": "src/a.py", "min_severity": "high"}),
    lambda c: c.update(expected={"action": "raise_nothing", "flag_file": "src/*.py", "must_mention_any": ["x"]}),
    lambda c: c.pop("finding"),
    lambda c: c["finding"].update(line=None),
    lambda c: c["finding"].update(line=0),
    lambda c: c.update(expected={"action": "skip_false_bug", "flag_file": "src/a.py"}, finding={"file": "src/a.py"}),
], ids=["no expected", "no category", "no notes", "no action", "no flag_file", "unknown action", "unknown key",
        "bad severity", "true bug without min_severity", "glob on a true bug", "glob on a false bug",
        "min_severity on a false bug", "must_mention_any on raise_nothing",
        "true bug without a finding", "true bug with no line", "true bug at line 0", "false bug with no line"])
def test_the_schema_rejects_a_broken_case(broken):
    bad = case("raise_true_bug", "src/a.py", min_severity="medium")
    broken(bad)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, SCHEMA)


@pytest.mark.parametrize("expected", [
    {"action": "raise_true_bug", "flag_file": "src/a.py", "min_severity": "medium", "must_mention_any": ["length"]},
    {"action": "skip_false_bug", "flag_file": "src/a.py"},
    {"action": "raise_nothing", "flag_file": "src/a.py"},
    {"action": "raise_nothing", "flag_file": "agents/**/*.py", "min_severity": "medium"},
])
def test_the_schema_accepts_each_action(expected):
    jsonschema.validate(case(expected["action"], expected["flag_file"]) | {"expected": expected}, SCHEMA)


@pytest.mark.parametrize("glob, path, matches", [
    ("src/*.py", "src/a.py", True),
    ("src/*.py", "src/sub/a.py", False),  # * stays within one folder
    ("src/**/*.py", "src/a.py", True),
    ("src/**/*.py", "src/sub/deep/a.py", True),
    ("**/*.py", "a.py", True),
    ("src/a.py", "src/a.py", True),
    ("src/a.py", "lib/src/a.py", False),  # the whole path, from the repo root
    ("src/a?.py", "src/ab.py", True),
])
def test_globs_follow_git_rules(glob, path, matches):
    assert bool(glob_regex(glob).match(path)) is matches


def test_a_clean_glob_over_another_cases_file_at_the_same_commit_is_a_contradiction():
    clean = case("raise_nothing", "src/**/*.py")
    bug = case("raise_true_bug", "src/sub/a.py", min_severity="medium")
    elsewhere = case("skip_false_bug", "lib/b.py")
    other_commit = case("raise_true_bug", "src/a.py", min_severity="high") | {"raised_sha": "b" * 40}
    also_clean = case("raise_nothing", "src/c.py")  # two clean cases agree

    assert contradictions([clean, bug, elsewhere, other_commit, also_clean]) == [(clean["case_id"], bug["case_id"])]
