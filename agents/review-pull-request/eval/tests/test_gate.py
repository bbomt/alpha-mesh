"""Acceptance tests for the regression gate (PMAT-312, AT3 and AT4).

The gate counts the development runs that pass their case's check and fails when the count drops more than the
tolerance below the baseline's. Results files are in Promptfoo's `-o results.json` format (see test_baseline.py).
"""

import pytest

import case_rules
import check_gate
from test_baseline import SHA, case, results, review, row

MODEL = "vendor/model:free"
IDS = [f"C{i}" for i in range(10)]


def cases():
    return [case(i) for i in IDS]  # skip_false_bug at line 10: a review with no finding at line 10 passes


def base(passing_runs: int, k: int = 3, model: str = MODEL) -> dict:
    """A baseline whose first `passing_runs` runs (case by case, repeat by repeat) pass."""
    entries, left = {}, passing_runs
    for case_id in IDS:
        runs = []
        for _ in range(k):
            runs.append("pass" if left > 0 else "fail")
            left -= 1
        entries[case_id] = {"runs": runs, "category": "logic", "action": "skip_false_bug"}
    return {"model": model, "k": k, "window": 3, "cases": entries}


def run_results(passing_runs: int, k: int = 3, error_at: str | None = None, skip: str | None = None) -> dict:
    rows, left = [], passing_runs
    for case_id in IDS:
        if case_id == skip:
            continue
        for repeat in range(k):
            if case_id == error_at and repeat == 0:
                rows.append(row(case_id, repeat, error="ReviewFailed: 429 Rate limit exceeded"))
            else:
                rows.append(row(case_id, repeat, review() if left > 0 else review(10)))
            left -= 1
    return results(*rows)


def gate(baseline, res, *, model=MODEL, k=3, tolerance=5):
    return check_gate.gate(baseline, res, {"model": model, "k": k}, cases(), tolerance=tolerance)


def test_a_drop_within_the_tolerance_passes():
    out = gate(base(20), run_results(15))
    assert out["passed"], out["reasons"]
    assert (out["baseline_passed"], out["run_passed"], out["runs"], out["drop"]) == (20, 15, 30, 5)


def test_a_drop_beyond_the_tolerance_fails_and_says_by_how_much():
    out = gate(base(20), run_results(14))
    assert not out["passed"]
    assert "runs passed 14/30 (baseline 20), drop 6 > tolerance 5" in out["reasons"]
    assert "runs passed 14/30 (baseline 20), drop 6 > tolerance 5" in check_gate.render(out)


def test_an_improvement_passes():
    assert gate(base(20), run_results(28))["passed"]


def test_a_harness_error_fails_even_when_the_count_is_fine():
    out = gate(base(10), run_results(30, error_at="C3"))
    assert not out["passed"]
    assert any("harness error" in r and "C3" in r for r in out["reasons"])


def test_a_run_on_another_model_than_the_baseline_fails():
    out = gate(base(20), run_results(30), model="other/model")
    assert not out["passed"]
    assert any("model changed" in r and "rebaseline" in r for r in out["reasons"])


def test_a_run_with_another_k_fails():
    out = gate(base(20), run_results(20, k=2), k=2)
    assert not out["passed"]
    assert any("k=2" in r and "k=3" in r for r in out["reasons"])


def test_a_case_missing_from_the_run_fails():
    out = gate(base(20), run_results(30, skip="C9"))
    assert not out["passed"]
    assert any("not in the run" in r and "C9" in r for r in out["reasons"])


def test_a_case_added_since_the_baseline_is_reported_not_gated():
    baseline = base(20)
    del baseline["cases"]["C9"]  # C9 is new: the baseline has no runs for it
    out = gate(baseline, run_results(20))
    assert out["passed"], out["reasons"]
    assert out["not_gated"] == ["C9"]
    assert out["runs"] == 27


def test_the_report_has_a_row_per_action_and_category_with_both_counts():
    text = check_gate.render(gate(base(20), run_results(15)))
    assert "| logic | `skip_false_bug` | 10 | 20/30 | 15/30 |" in text


# AT4: development cases only ───────────────────────────────────────────────────────────────────

def test_the_gate_case_list_holds_no_holdout_case():
    ids = {c["case_id"] for c in check_gate.gate_cases()}
    assert ids, "the gate must run on some cases"
    assert not ids & case_rules.holdout_ids()
    assert ids == {c["case_id"] for c in case_rules.development_cases()}


def test_results_holding_a_holdout_case_are_refused():
    held = sorted(case_rules.holdout_ids())[0]
    res = run_results(30)
    res["results"]["results"].append(row(held, 0, review()))
    with pytest.raises(ValueError, match="hold-out"):
        gate(base(20), res)
