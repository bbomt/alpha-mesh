"""The regression gate (PMAT-312): does an eval run keep the baseline's score, within the noise?

    uv run python eval/scripts/score_eval_run.py eval/runs/<run> [--tolerance N] [--markdown <file>] [--quick]

It counts the development runs (case x repeat) that pass their case's line check and fails when that count drops
more than the tolerance below the baseline's, on the cases both hold. It counts runs, not cases: between two runs of
the same code over a third of the cases change in some repeat, but the total barely moves (PMAT-312's noise test), so a case-level
rule would fail at random.

It also fails, whatever the count, on a harness error, a missing case, or a run on another model or k than the
baseline's: those runs measure something else, and a model change needs a new baseline.

The judge is a second gate (PMAT-340): it fails when the judge's mean correctness or coverage drops more than
JUDGE_TOLERANCE below the baseline's, on the same cases. A judge error is never a score; more than
MAX_JUDGE_ERROR_RATE of the runs failing to be judged fails the run as a harness error, and so does a run judged by
another judge model or rubric than the baseline's, or with repeats not judged.

Reported, never gating yet (the owner switches them on): security misses, failing style_only runs, and the judge's
means against PMAT-311's provisional proposal. Exits 1 when the gate fails.

--quick scores a k=1 run against the baseline's first repeat, with the tolerance scaled to the smaller run
(quick_tolerance). It is a cheap check for large drops, never the gate.

--cases <file> scores only the cases a list ({"cases": [...]}) names, the tolerances scaled to their share of the
development cases: the smoke evals (mesh run-evals --smoke, with --quick) and the eval-rc-q drop loop's runs of a
failed gate's cases (PMAT-358). --regressed <file> writes the cases that passed fewer runs than the baseline, or had a
harness error: the cases the drop loop re-runs and bisects on.
"""

import argparse
import json
import math
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "promptfoo")]
import write_baseline as bl  # noqa: E402
import case_rules  # noqa: E402
import judge_report  # noqa: E402

# Runs the count may drop below the baseline's before the gate fails. Provisional: three runs of the same code
# (base6, aa1, aa2) passed 140, 142 and 146 of 195; 10 is about 5 points. Tighten it as same-code runs come in.
TOLERANCE = 10
# The drop in the judge's mean (1 to 5) the gate allows, per dimension (PMAT-340's noise test). Every repeat of the
# three same-code runs judged: correctness 3.01, 2.96, 2.87 and coverage 3.77, 3.80, 3.77. One repeat's mean has a
# standard deviation of 0.145 (correctness) and 0.072 (coverage), so two k=3 means differ by about 0.12 and 0.06 at one
# standard deviation; each tolerance is about two. Provisional: tighten them as same-code runs come in.
JUDGE_TOLERANCE = {"correctness": 0.25, "coverage": 0.12}
# More judge errors than this share of the runs fail the run as a harness error: the means would miss too much of it.
# The noise test had 2 of 195 (1%) in each run; base6's first judging, which refetched files from GitHub, had 10 (5%).
MAX_JUDGE_ERROR_RATE = 0.05
REBASELINE_NEW_CASES = 5  # suggest a rebaseline once this many cases are new since the baseline (not gated)


def quick_tolerance(tolerance: int, baseline_k: int, share: float = 1.0) -> int:
    """The tolerance for a k=1 run: the noise in a count grows with the square root of the runs, so a third of the
    runs gets about 1/sqrt(3) of the tolerance (10 -> 6). A single repeat is noisy: the 9 repeats of the three
    same-code runs passed 43 to 54 of 65, so about 1 quick check in 10 warns on unchanged code. A run of `share` of
    the cases (the smoke set) scales it again: 19 of 65 cases gives 10 -> 3."""
    return round(tolerance * math.sqrt(share / baseline_k))


def judge_gate(base: dict | None, run: dict, tolerance: dict[str, float]) -> tuple[list[str], list[dict]]:
    """The judge's rule: the failure reasons, and one row per gated dimension for the report."""
    if base is None:
        return ["the baseline has no judge means: rebaseline (write_baseline.py) before gating"], []
    reasons, rows = [], []
    if (run["model"], run["rubric"]) != (base["model"], base["rubric"]) and run["judged"]:
        reasons.append(f"judge changed: the run was judged by {run['model']} (rubric {run['rubric']}), the baseline "
                       f"by {base['model']} (rubric {base['rubric']}); rebaseline with the new judge before gating")
    if run["not_judged"]:
        reasons.append(f"{run['not_judged']} of {run['runs']} runs not judged: the gate needs every repeat judged "
                       "(run_eval.sh --judge-repeats all)")
    if run["runs"] and run["judge_errors"] / run["runs"] > MAX_JUDGE_ERROR_RATE:
        reasons.append(f"harness error: the judge failed on {run['judge_errors']} of {run['runs']} runs, more than "
                       f"{MAX_JUDGE_ERROR_RATE:.0%}; run the judge again (run_eval.sh --stored)")
    for d, tol in tolerance.items():
        b, r = base["mean"][d], run["mean"][d]
        if b is None or r is None:
            reasons.append(f"no judge mean {d} to compare (baseline {b}, run {r})")
            continue
        needed = round(b - tol, 2)
        rows.append({"dimension": d, "run": r, "baseline": b, "needed": needed, "tolerance": tol})
        if round(r, 2) < needed:
            reasons.append(f"judge mean {d} {r:.2f}, below the {needed:.2f} needed "
                           f"(the baseline's {b:.2f} minus the tolerance {tol:.2f})")
    return reasons, rows


def quick_judge_tolerance(tolerance: float, baseline_k: int, share: float = 1.0) -> float:
    """The judge tolerance for a k=1 run: a mean over a third of the runs is about sqrt(3) times as noisy, and a mean
    over `share` of the cases about 1/sqrt(share) times as noisy again."""
    return round(tolerance * math.sqrt(baseline_k / share), 2)


def gate_cases() -> list[dict]:
    """The cases the gate runs on: the development cases, never the hold-out."""
    return case_rules.development_cases()


def smoke_cases(path: Path) -> list[dict]:
    """The development cases a smoke list (eval/smoke.json) names; exits on a name that is not a development case."""
    names = json.loads(path.read_text())["cases"]
    if not names:
        sys.exit(f"{path}: no cases")
    dev = {c["case_id"]: c for c in gate_cases()}
    if bad := sorted(set(names) - set(dev)):
        sys.exit(f"{path}: not development cases: {', '.join(bad)}")
    return [dev[n] for n in names]


def gate(baseline: dict, results: dict, run: dict, cases: list[dict], tolerance: int = TOLERANCE,
         quick: bool = False, judge_tolerance: dict[str, float] | None = None, share: float = 1.0) -> dict:
    held = case_rules.holdout_ids()
    if found := sorted({r["vars"]["case_id"] for r in results["results"]["results"]} & held):
        raise ValueError(f"{', '.join(found)}: hold-out cases; the gate reads development results only")

    by_id = {c["case_id"]: c for c in cases}
    runs = bl.runs(results, cases, baseline.get("window", case_rules.WINDOW))
    reasons = []
    if run["model"] != baseline["model"]:
        reasons.append(f"model changed: the run used {run['model']}, the baseline {baseline['model']}; "
                       "rebaseline on the new model before gating")
    if quick:
        if run["k"] != 1:
            reasons.append(f"a quick run has k=1, this one k={run['k']}")
    elif run["k"] != baseline["k"]:
        reasons.append(f"the run has k={run['k']}, the baseline k={baseline['k']}")
    if quick or share < 1:  # fewer runs than the full gate: the noise, and so the tolerance, scale with sqrt(runs)
        tolerance = quick_tolerance(tolerance, baseline["k"] if quick else 1, share)
    if errors := sorted(c for c, rs in runs.items() if any(r["result"] == "error" for r in rs)):
        reasons.append(f"harness error in {len(errors)} case(s): {', '.join(errors)}")
    if missing := sorted(set(by_id) - set(runs)):
        reasons.append(f"{len(missing)} case(s) not in the run: {', '.join(missing)}")
    if short := sorted(c for c, rs in runs.items() if len(rs) != run["k"]):
        reasons.append(f"{len(short)} case(s) without {run['k']} repeats: {', '.join(short)}")

    gated = sorted(c for c in runs if c in baseline["cases"])
    not_gated = sorted(c for c in runs if c not in baseline["cases"])
    rows: dict[tuple, dict] = {}
    for case_id in gated:
        case = by_id[case_id]
        key = (bl.category_group(case), case["expected"]["action"])
        r = rows.setdefault(key, {"cases": 0, "baseline": 0, "run": 0, "runs": 0})
        r["cases"] += 1
        base_runs = baseline["cases"][case_id]["runs"][:1] if quick else baseline["cases"][case_id]["runs"]
        r["baseline"] += base_runs.count("pass")
        r["run"] += [x["result"] for x in runs[case_id]].count("pass")
        r["runs"] += len(runs[case_id])

    baseline_passed = sum(r["baseline"] for r in rows.values())
    run_passed = sum(r["run"] for r in rows.values())
    total = sum(r["runs"] for r in rows.values())
    drop = baseline_passed - run_passed
    needed = baseline_passed - tolerance
    change = f"{-drop:+d}" if drop else "no change"
    count = (f"runs passed {run_passed}/{total} · baseline {baseline_passed}/{total} · "
             f"needs at least {needed}/{total} (tolerance {tolerance}) · change {change}")
    if drop > tolerance:
        reasons.append(f"runs passed {run_passed}/{total}, below the {needed}/{total} needed "
                       f"(the baseline's {baseline_passed} minus the tolerance {tolerance})")
    # The cases to bisect on: only when the run count failed (or a harness error), those that lost runs or errored.
    # A failure on the judge's means alone names none: the baseline holds means, not per-case scores (PMAT-358 R2).
    regressed = sorted(c for c in gated if [x["result"] for x in runs[c]].count("pass") < (
        baseline["cases"][c]["runs"][:1] if quick else baseline["cases"][c]["runs"]).count("pass")
        or any(x["result"] == "error" for x in runs[c])) if drop > tolerance or errors else []

    def failing(pred):
        return sum(x["result"] == "fail" for c in gated if pred(by_id[c]) for x in runs[c])

    collected = judge_report.collect(results, cases)
    judge_tolerance = dict(judge_tolerance or JUDGE_TOLERANCE)
    if quick or share < 1:
        judge_tolerance = {d: quick_judge_tolerance(t, baseline["k"] if quick else 1, share)
                           for d, t in judge_tolerance.items()}
    judge_reasons, judge_rows = judge_gate(baseline.get("judge"),
                                           judge_report.gate_means([e for e in collected
                                                                    if e["case"]["case_id"] in gated]),
                                           judge_tolerance)
    reasons += judge_reasons

    return {
        "passed": not reasons, "reasons": reasons, "count": count, "tolerance": tolerance,
        "baseline_passed": baseline_passed, "run_passed": run_passed, "runs": total, "drop": drop,
        "rows": {f"{g}|{a}": r for (g, a), r in sorted(rows.items())}, "not_gated": not_gated,
        "security_misses": failing(lambda c: c["category"] == "security" and c["expected"]["action"] == "raise_true_bug"),
        "style_only_failures": failing(lambda c: c["category"] == "style_only"),
        "judge": judge_report.summarise(collected), "judge_rows": judge_rows,
        "model": run["model"], "k": run["k"], "quick": quick, "smoke": len(cases) if share < 1 else None,
        "rebaseline": [] if quick else rebaseline_hints(drop, tolerance, not_gated),
        "regressed": regressed,
    }


def rebaseline_hints(drop: int, tolerance: int, not_gated: list[str]) -> list[str]:
    """When the report should suggest a rebaseline (eval/README.md, Rebaselining). Never after a drop: that would
    lower the bar."""
    hints = []
    if drop <= -tolerance:
        hints.append(f"this run passes {-drop} more runs than the baseline (at least the tolerance): rebaseline in this "
                     "PR to raise the bar")
    if len(not_gated) >= REBASELINE_NEW_CASES:
        hints.append(f"{len(not_gated)} cases are new since the baseline and not gated: rebaseline to gate them")
    return hints


def render(out: dict) -> str:
    fmt = lambda v: "n/a" if v is None else f"{v:.2f}"  # noqa: E731
    verdict = "PASS" if out["passed"] else "FAIL"
    title = "quick check (k=1, an early warning, not the gate)" if out["quick"] else "evals gate"
    if out.get("smoke"):
        title = f"smoke evals (k=1 on {out['smoke']} cases, not the full gate)"
    lines = [f"## {title}: {verdict}", "", out["count"], "",
             f"Model `{out['model']}`, k={out['k']}. A run is one review of one case; it passes when the review does "
             "what the case expects. The baseline is the agent as it is on master, not a target.", ""]
    if out["reasons"]:
        lines += ["**Failed because:**", ""] + [f"- {r}" for r in out["reasons"]] + [""]
    lines += ["| Category | Expected action | Cases | Baseline runs passed | Run runs passed |",
              "| :-- | :-- | --: | --: | --: |"]
    for key, r in out["rows"].items():
        group, action = key.split("|")
        lines.append(f"| {group} | `{action}` | {r['cases']} | {r['baseline']}/{r['runs']} | {r['run']}/{r['runs']} |")
    if out["judge_rows"]:
        lines += ["", "| Judge mean (1-5) | Baseline | Run | Needs at least | Tolerance |", "| :-- | --: | --: | --: | --: |"]
        lines += [f"| {r['dimension']} | {r['baseline']:.2f} | {r['run']:.2f} | {r['needed']:.2f} | {r['tolerance']:.2f} |"
                  for r in out["judge_rows"]]
    if out["rebaseline"]:
        lines += ["", "**Rebaseline suggested:**", ""] + [f"- {h}" for h in out["rebaseline"]]
    if out["not_gated"]:
        lines += ["", f"New since the baseline, not gated: {', '.join(out['not_gated'])}"]
    j = out["judge"]
    lines += ["", "**Reported, not gating yet** (provisional, PMAT-311):", "",
              f"- security misses (failing runs): {out['security_misses']} (proposal 0)",
              f"- failing style_only runs: {out['style_only_failures']} (proposal 0)"]
    for d in judge_report.DIMENSIONS:
        goal = f" (proposal >= {judge_report.PROPOSAL[d]})" if d in judge_report.PROPOSAL else ""
        gated = " (gated against the baseline above)" if d in {r["dimension"] for r in out["judge_rows"]} else ""
        lines.append(f"- judge mean {d}: {fmt(j['mean'][d])}{goal}{gated}")
    lines.append(f"- judged runs: {j['runs'] - j['not_judged'] - j['judge_errors']} of {j['runs']}, "
                 f"judge errors {j['judge_errors']}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The regression gate: compare an eval run with the baseline.")
    parser.add_argument("run_dir", type=Path, help="eval/runs/<run>, holding results.json and run.json")
    parser.add_argument("--baseline", type=Path, default=bl.BASELINE)
    parser.add_argument("--tolerance", type=int, default=TOLERANCE)
    parser.add_argument("--markdown", type=Path, help="also write the report to this file")
    parser.add_argument("--quick", action="store_true", help="score a k=1 run against the baseline's first repeat")
    parser.add_argument("--cases", type=Path, help="score only the cases this list ({\"cases\": [...]}) names")
    parser.add_argument("--regressed", type=Path, help="write the cases that lost runs against the baseline here")
    args = parser.parse_args(argv)

    cases = all_cases = gate_cases()
    if args.cases:
        cases = smoke_cases(args.cases)
    out = gate(json.loads(args.baseline.read_text()), json.loads((args.run_dir / "results.json").read_text()),
               json.loads((args.run_dir / "run.json").read_text()), cases, tolerance=args.tolerance,
               quick=args.quick, share=len(cases) / len(all_cases))
    text = render(out)
    print(text)
    if args.markdown:
        args.markdown.write_text(text)
    if args.regressed:
        args.regressed.write_text(json.dumps(out["regressed"]) + "\n")
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
