"""Acceptance tests for giving mined cases a real category (PMAT-321). The numbered tests are the ticket's."""

import json

import baseline
import case_rules
import split_holdout

EVAL = case_rules.EVAL
SCHEMA = json.loads((EVAL / "schema" / "case.schema.json").read_text())
CATEGORIES = set(SCHEMA["properties"]["category"]["enum"])
CASES = case_rules.load_cases()
MINED = [c for c in CASES if c["source"] == "pr_comment"]
# holdout.json as PMAT-318 committed it (845be4f). The recategorisation must not move a case to the other side.
HOLDOUT_AT_PMAT_318 = ["R41-72f59a5-001", "R41-72f59a5-002", "con-002", "deg-003", "imp-002", "log-003", "log-006",
                       "pub-nvm-2439", "pub-vscode-131998", "pub-vscode-131998-2", "ref-002", "sec-003", "sec-006",
                       "sty-001", "sty-003"]


# 1 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_1_no_case_and_no_part_of_the_schema_uses_mined_as_a_category():
    assert "mined" not in CATEGORIES
    assert not [c["case_id"] for c in CASES if c["category"] == "mined"]


def test_1_every_mined_case_has_a_real_category():
    assert len(MINED) == 32
    assert all(c["category"] in CATEGORIES for c in MINED), [c["case_id"] for c in MINED]


# 2 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_2_the_holdout_list_is_unchanged_and_the_draw_still_gives_it():
    committed = json.loads(case_rules.HOLDOUT.read_text())
    assert committed == {"seed": 318, "cases": HOLDOUT_AT_PMAT_318}
    assert split_holdout.draw(CASES, committed["seed"]) == HOLDOUT_AT_PMAT_318


# 3 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_3_the_baselines_pass_rates_are_unchanged_and_only_the_grouping_moves():
    b = json.loads((EVAL / "baseline.json").read_text())
    by_id = {c["case_id"]: c for c in CASES}
    s = b["summary"]
    assert (s["cases"], s["pass_rate"], s["pass_k"], s["file_level_pass_rate"]) == (65, 0.7128, 0.5231, 0.6821)
    assert all(e["category"] == by_id[i]["category"] for i, e in b["cases"].items())
    assert s["by_category"] == baseline.pass_rates(b["cases"], by_id, baseline.category_group)

    mined = {}
    for key, row in s["by_category"].items():
        if key.startswith("mined:"):
            totals = mined.setdefault(key.rsplit("/", 1)[1], {"cases": 0, "runs_passed": 0, "runs": 0, "passed_all": 0})
            for k in totals:
                totals[k] += row[k]
    assert mined == {  # the old `mined/<action>` rows of the PMAT-318 baseline
        "skip_false_bug": {"cases": 20, "runs_passed": 38, "runs": 60, "passed_all": 7},
        "raise_true_bug": {"cases": 10, "runs_passed": 10, "runs": 30, "passed_all": 0}}
