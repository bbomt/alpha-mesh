"""The smoke set (PMAT-358): eval/smoke.json, the cases `mesh run-evals --smoke` runs at k=1 on every agent PR.

It holds development cases only (never a hold-out case), every development security and style_only case, and some
from each folder. score_eval_run.py --cases scores a run of just these against the baseline's first repeat.
"""

import json

import pytest

import case_rules
import score_eval_run
from test_score_eval_run import IDS, JUDGE_TOLERANCE, MODEL, base, cases, run_results

SMOKE = json.loads((case_rules.EVAL / "smoke.json").read_text())["cases"]


def test_the_smoke_set_is_development_cases_only_and_never_a_holdout_case():
    dev = {c["case_id"] for c in case_rules.development_cases()}
    assert len(SMOKE) == len(set(SMOKE)), "a case is listed twice"
    assert not set(SMOKE) & case_rules.holdout_ids()
    assert set(SMOKE) <= dev, f"not development cases: {sorted(set(SMOKE) - dev)}"
    assert 15 <= len(SMOKE) <= 20


def test_the_smoke_set_has_every_security_and_style_only_case_and_some_from_each_folder():
    dev = case_rules.development_cases()
    for category in ("security", "style_only"):
        assert {c["case_id"] for c in dev if c["category"] == category} <= set(SMOKE), category
    folders = {c["case_id"]: p.parent.name for p in case_rules.CASES_DIR.glob("*/*.json")
               for c in [json.loads(p.read_text())]}
    assert {folders[i] for i in SMOKE} == {"alpha-mesh", "public", "synthetic"}


def smoke(baseline, res, subset, **kw):
    return score_eval_run.gate(baseline, res, {"model": MODEL, "k": 1}, [c for c in cases() if c["case_id"] in subset],
                               tolerance=10, quick=True, judge_tolerance=JUDGE_TOLERANCE,
                               share=len(subset) / len(IDS), **kw)


def keep(res, subset):
    res["results"]["results"] = [r for r in res["results"]["results"] if r["vars"]["case_id"] in subset]
    return res


def test_a_smoke_run_is_scored_on_its_cases_only_with_the_tolerance_scaled_to_its_share():
    subset = IDS[:3]                 # 3 of 10 cases at k=1 against a k=3 baseline: 10 * sqrt(0.3 / 3) = 3.2 -> 3
    out = smoke(base(30), keep(run_results(3, k=1), subset), subset)
    assert out["passed"], out["reasons"]
    assert (out["runs"], out["baseline_passed"], out["tolerance"]) == (3, 3, 3)
    assert not any("not in the run" in r for r in out["reasons"])  # the other 7 cases are not missing
    assert out["judge_rows"][0]["tolerance"] == pytest.approx(round(0.3 * (3 / 0.3) ** 0.5, 2))
    assert "smoke evals (k=1 on 3 cases" in score_eval_run.render(out)


def test_a_smoke_run_still_fails_on_a_missing_smoke_case():
    subset = IDS[:3]
    out = smoke(base(30), keep(run_results(3, k=1), IDS[:2]), subset)
    assert not out["passed"] and any("C2" in r and "not in the run" in r for r in out["reasons"])


def test_the_cli_refuses_a_smoke_list_with_a_case_that_is_not_a_development_case(tmp_path):
    bad = tmp_path / "smoke.json"
    bad.write_text(json.dumps({"cases": [sorted(case_rules.holdout_ids())[0]]}))
    with pytest.raises(SystemExit, match="not development cases"):
        score_eval_run.smoke_cases(bad)
