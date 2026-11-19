"""Acceptance tests for the regression gate (PMAT-312, AT3 and AT4).

The gate counts the development runs that pass their case's check and fails when the count drops more than the
tolerance below the baseline's. Results files are in Promptfoo's `-o results.json` format (see test_write_baseline.py).
"""

import pytest

import case_rules
import score_eval_run
from test_write_baseline import JUDGE, SHA, case, judge_block, judged, results, review, row

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
    return {"model": model, "k": k, "window": 3, "cases": entries, "judge": judge_block(runs=10 * k)}


def run_results(passing_runs: int, k: int = 3, error_at: str | None = None, skip: str | None = None,
                scores=(3, 4, 5), judge_errors: int = 0, **judge) -> dict:
    """Every row judged with `scores` (the baseline's means by default), the first `judge_errors` rows with a judge
    error instead."""
    rows, left = [], passing_runs
    for case_id in IDS:
        if case_id == skip:
            continue
        for repeat in range(k):
            if case_id == error_at and repeat == 0:
                rows.append(row(case_id, repeat, error="ReviewFailed: 429 Rate limit exceeded"))
            else:
                r = row(case_id, repeat, review() if left > 0 else review(10))
                rows.append(judged(r, error="no score in the reply") if len(rows) < judge_errors
                            else judged(r, *scores, **judge))
            left -= 1
    return results(*rows)


JUDGE_TOLERANCE = {"correctness": 0.3, "coverage": 0.2}


@pytest.fixture(autouse=True)
def _max_judge_error_rate(monkeypatch):
    monkeypatch.setattr(score_eval_run, "MAX_JUDGE_ERROR_RATE", 0.1)


def gate(baseline, res, *, model=MODEL, k=3, tolerance=5):
    return score_eval_run.gate(baseline, res, {"model": model, "k": k}, cases(), tolerance=tolerance,
                               judge_tolerance=JUDGE_TOLERANCE)


def test_a_drop_within_the_tolerance_passes():
    out = gate(base(20), run_results(15))
    assert out["passed"], out["reasons"]
    assert (out["baseline_passed"], out["run_passed"], out["runs"], out["drop"]) == (20, 15, 30, 5)


def test_a_drop_beyond_the_tolerance_fails_and_says_what_was_needed():
    out = gate(base(20), run_results(14))
    assert not out["passed"]
    why = "runs passed 14/30, below the 15/30 needed (the baseline's 20 minus the tolerance 5)"
    assert why in out["reasons"]
    text = score_eval_run.render(out)
    assert why in text
    assert "runs passed 14/30 · baseline 20/30 · needs at least 15/30 (tolerance 5) · change -6" in text


def test_exactly_the_tolerance_below_the_baseline_still_passes():
    out = gate(base(20), run_results(15))
    assert out["passed"] and "needs at least 15/30" in out["count"]


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
    text = score_eval_run.render(gate(base(20), run_results(15)))
    assert "| logic | `skip_false_bug` | 10 | 20/30 | 15/30 |" in text


# AT4: development cases only ───────────────────────────────────────────────────────────────────

def test_the_gate_case_list_holds_no_holdout_case():
    ids = {c["case_id"] for c in score_eval_run.gate_cases()}
    assert ids, "the gate must run on some cases"
    assert not ids & case_rules.holdout_ids()
    assert ids == {c["case_id"] for c in case_rules.development_cases()}


def test_results_holding_a_holdout_case_are_refused():
    held = sorted(case_rules.holdout_ids())[0]
    res = run_results(30)
    res["results"]["results"].append(row(held, 0, review()))
    with pytest.raises(ValueError, match="hold-out"):
        gate(base(20), res)


# The quick check (mesh run-evals --quick): k=1 against the baseline's first repeat ───────────────────────────────

def quick(baseline, res, **kw):
    return score_eval_run.gate(baseline, res, {"model": MODEL, "k": kw.get("k", 1)}, cases(), tolerance=5, quick=True,
                               judge_tolerance=JUDGE_TOLERANCE)


def test_quick_compares_a_k1_run_with_the_baselines_first_repeat_and_a_scaled_tolerance():
    baseline = base(20)  # first repeats: C0-C6 pass, 7 of 10
    assert score_eval_run.quick_tolerance(5, 3) == 3 and score_eval_run.quick_tolerance(10, 3) == 6
    ok = quick(baseline, run_results(4, k=1))
    assert ok["passed"] and (ok["baseline_passed"], ok["runs"], ok["tolerance"]) == (7, 10, 3)
    assert not quick(baseline, run_results(3, k=1))["passed"]
    assert "quick check" in score_eval_run.render(ok)


def test_quick_refuses_a_run_with_more_repeats():
    out = quick(base(20), run_results(30), k=3)
    assert not out["passed"] and any("k=1" in r for r in out["reasons"])


# When to rebaseline ───────────────────────────────────────────────────────────────────────────────────────────────

def test_a_rise_of_at_least_the_tolerance_suggests_a_rebaseline():
    out = gate(base(10), run_results(15))
    assert out["rebaseline"] and "raise the bar" in score_eval_run.render(out)
    assert not gate(base(10), run_results(14))["rebaseline"]


def test_enough_new_cases_suggest_a_rebaseline_and_a_drop_never_does():
    baseline = base(20)
    for case_id in IDS[5:]:
        del baseline["cases"][case_id]
    assert any("5 cases are new" in h for h in gate(baseline, run_results(30))["rebaseline"])
    assert not gate(base(20), run_results(16))["rebaseline"]


# The judge as a second gate (PMAT-340) ──────────────────────────────────────────────────────────────────────────
# The baseline's judge means are correctness 3.0, coverage 4.0 (judge_block); the tolerances 0.3 and 0.2.

@pytest.mark.parametrize("scores, dimension", [((2.7, 4, 5), "correctness"), ((3, 3.8, 5), "coverage")])
def test_a_judge_mean_drop_within_its_tolerance_passes(scores, dimension):
    out = gate(base(20), run_results(20, scores=scores))
    assert out["passed"], out["reasons"]


@pytest.mark.parametrize("scores, why", [
    ((2.6, 4, 5), "judge mean correctness 2.60, below the 2.70 needed (the baseline's 3.00 minus the tolerance 0.30)"),
    ((3, 3.7, 5), "judge mean coverage 3.70, below the 3.80 needed (the baseline's 4.00 minus the tolerance 0.20)"),
], ids=["correctness", "coverage"])
def test_a_judge_mean_drop_beyond_its_tolerance_fails_even_when_the_count_is_fine(scores, why):
    out = gate(base(20), run_results(30, scores=scores))
    assert not out["passed"]
    assert out["reasons"] == [why]
    assert why in score_eval_run.render(out)


def test_actionability_is_reported_not_gated():
    assert gate(base(20), run_results(20, scores=(3, 4, 1)))["passed"]


def test_the_report_shows_both_judge_means_against_the_baseline():
    text = score_eval_run.render(gate(base(20), run_results(20, scores=(2.8, 4.1, 5))))
    assert "| correctness | 3.00 | 2.80 | 2.70 | 0.30 |" in text
    assert "| coverage | 4.00 | 4.10 | 3.80 | 0.20 |" in text


def test_a_judge_error_is_never_a_low_score():
    """3 of 30 rows (10%, the limit) have a judge error: the means are over the 27 judged rows, so nothing drops."""
    out = gate(base(20), run_results(20, judge_errors=3))
    assert out["passed"], out["reasons"]
    assert out["judge_rows"][0]["run"] == 3.0


def test_too_many_judge_errors_fail_the_run_as_a_harness_error():
    out = gate(base(20), run_results(20, judge_errors=4))
    assert not out["passed"]
    assert any(r.startswith("harness error: the judge failed on 4 of 30 runs") for r in out["reasons"])


def test_a_run_with_repeats_not_judged_fails():
    res = run_results(20)
    res["results"]["results"][5].pop("gradingResult")
    out = gate(base(20), res)
    assert not out["passed"] and any("1 of 30 runs not judged" in r for r in out["reasons"])


@pytest.mark.parametrize("change", [{"model": "other/judge"}, {"rubric": "v2"}])
def test_a_run_judged_by_another_judge_or_rubric_fails(change):
    out = gate(base(20), run_results(20, **change))
    assert not out["passed"] and any("judge changed" in r and "rebaseline" in r for r in out["reasons"])


def test_a_baseline_without_judge_means_fails():
    baseline = base(20)
    del baseline["judge"]
    out = gate(baseline, run_results(20))
    assert not out["passed"] and any("no judge means" in r for r in out["reasons"])


def test_quick_scales_the_judge_tolerance_up_for_one_repeat():
    """A mean over a third of the runs is about sqrt(3) times as noisy: 0.3 -> 0.52, 0.2 -> 0.35."""
    assert quick(base(20), run_results(4, k=1, scores=(2.5, 3.7, 5)))["passed"]
    out = quick(base(20), run_results(4, k=1, scores=(2.4, 4, 5)))
    assert not out["passed"] and any("judge mean correctness 2.40, below the 2.48 needed" in r for r in out["reasons"])
