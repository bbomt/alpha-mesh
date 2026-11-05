"""The dataset covers every category in the spec, and every hand-written case can run (PMAT-310)."""

import collections
import json

import pytest

import eval_cases

MINIMUMS = {"logic": 8, "security": 8, "style_only": 6, "refactor": 4, "concurrency": 4, "improvable": 4, "degenerate": 3}
FOLDER = eval_cases.CASES_DIR


def cases_in(folder):
    return [json.loads(p.read_text()) for p in sorted((FOLDER / folder).glob("*.json"))]


SYNTHETIC, PUBLIC = cases_in("synthetic"), cases_in("public")
TOTAL = len(eval_cases.case_files(FOLDER))


@pytest.mark.parametrize("category, minimum", MINIMUMS.items())
def test_every_synthetic_category_has_its_minimum_count(category, minimum):
    assert sum(c["category"] == category for c in SYNTHETIC) >= minimum


def test_the_dataset_has_about_10_public_cases_and_50_to_100_cases_in_all():
    assert len(PUBLIC) >= 8
    assert 50 <= TOTAL <= 100


@pytest.mark.parametrize("case", SYNTHETIC + PUBLIC, ids=lambda c: c["case_id"])
def test_every_hand_written_case_can_run_and_says_why_it_exists(case):
    assert case["base_sha"] and case["raised_sha"] and case["notes"].strip()
    assert "pr_number" in case or "description" in case


def test_synthetic_cases_follow_the_category_rules():
    rules = {"logic": ("raise_true_bug", "medium", None), "security": ("raise_true_bug", "high", None),
             "concurrency": ("raise_true_bug", "medium", None), "improvable": ("raise_true_bug", "low", None),
             "style_only": ("raise_nothing", None, None), "degenerate": ("raise_nothing", None, None),
             "refactor": ("raise_nothing", "medium", 3)}
    for c in SYNTHETIC:
        e = c["expected"]
        assert (e["action"], e.get("min_severity"), e.get("max_comments")) == rules[c["category"]], c["case_id"]


def test_every_synthetic_case_has_its_own_commit():
    commits = collections.Counter(c["raised_sha"] for c in SYNTHETIC)
    assert max(commits.values()) == 1


def test_no_synthetic_commit_message_or_description_names_the_bug_or_the_eval():
    banned = ("bug", "vulnerab", "injection", "traversal", "race", "hallucin", "planted", "eval", "fixture")
    for c in SYNTHETIC:
        assert not any(word in c["description"].lower() for word in banned), c["case_id"]
