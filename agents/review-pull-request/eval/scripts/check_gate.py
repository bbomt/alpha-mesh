"""The regression gate (PMAT-312): does an eval run keep the baseline's score, within the noise?

    uv run python eval/scripts/check_gate.py eval/runs/<run> [--tolerance N] [--markdown <file>]

It counts the development runs (case x repeat) that pass their case's line check and fails when that count drops
more than the tolerance below the baseline's, on the cases both hold. It counts runs, not cases: between two runs of
the same code about 1 case in 5 changes result, but the total barely moves (A/A test, PMAT-312), so a case-level
rule would fail at random.

It also fails, whatever the count, on a harness error, a missing case, or a run on another model or k than the
baseline's: those runs measure something else, and a model change needs a new baseline.

Reported, never gating yet (the owner switches them on): security misses, failing style_only runs, and the judge's
means against PMAT-311's provisional proposal. Exits 1 when the gate fails.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "promptfoo")]
import baseline as bl  # noqa: E402
import case_rules  # noqa: E402
import judge_report  # noqa: E402

# Runs the count may drop below the baseline's before the gate fails. Provisional: base6 and the A/A run aa1 of the
# same code differed by 2 of 195 (140 vs 142); 10 is about 5 points. Tighten it as same-code runs come in.
TOLERANCE = 10


def gate_cases() -> list[dict]:
    """The cases the gate runs on: the development cases, never the hold-out."""
    return case_rules.development_cases()


def gate(baseline: dict, results: dict, run: dict, cases: list[dict], tolerance: int = TOLERANCE) -> dict:
    held = case_rules.holdout_ids()
    if found := sorted({r["vars"]["case_id"] for r in results["results"]["results"]} & held):
        raise ValueError(f"{', '.join(found)}: hold-out cases; the gate reads development results only")

    by_id = {c["case_id"]: c for c in cases}
    runs = bl.runs(results, cases, baseline.get("window", case_rules.WINDOW))
    reasons = []
    if run["model"] != baseline["model"]:
        reasons.append(f"model changed: the run used {run['model']}, the baseline {baseline['model']}; "
                       "rebaseline on the new model before gating")
    if run["k"] != baseline["k"]:
        reasons.append(f"the run has k={run['k']}, the baseline k={baseline['k']}")
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
        r["baseline"] += baseline["cases"][case_id]["runs"].count("pass")
        r["run"] += [x["result"] for x in runs[case_id]].count("pass")
        r["runs"] += len(runs[case_id])

    baseline_passed = sum(r["baseline"] for r in rows.values())
    run_passed = sum(r["run"] for r in rows.values())
    total = sum(r["runs"] for r in rows.values())
    drop = baseline_passed - run_passed
    count = f"runs passed {run_passed}/{total} (baseline {baseline_passed}), drop {drop}"
    if drop > tolerance:
        reasons.append(f"{count} > tolerance {tolerance}")

    def failing(pred):
        return sum(x["result"] == "fail" for c in gated if pred(by_id[c]) for x in runs[c])

    return {
        "passed": not reasons, "reasons": reasons, "count": count, "tolerance": tolerance,
        "baseline_passed": baseline_passed, "run_passed": run_passed, "runs": total, "drop": drop,
        "rows": {f"{g}|{a}": r for (g, a), r in sorted(rows.items())}, "not_gated": not_gated,
        "security_misses": failing(lambda c: c["category"] == "security" and c["expected"]["action"] == "raise_true_bug"),
        "style_only_failures": failing(lambda c: c["category"] == "style_only"),
        "judge": judge_report.summarise(judge_report.collect(results, cases)),
        "model": run["model"], "k": run["k"],
    }


def render(out: dict) -> str:
    fmt = lambda v: "n/a" if v is None else f"{v:.2f}"  # noqa: E731
    verdict = "PASS" if out["passed"] else "FAIL"
    lines = [f"## evals gate: {verdict}", "",
             f"{out['count']}, tolerance {out['tolerance']} (model `{out['model']}`, k={out['k']})", ""]
    if out["reasons"]:
        lines += ["**Failed because:**", ""] + [f"- {r}" for r in out["reasons"]] + [""]
    lines += ["| Category | Expected action | Cases | Baseline runs passed | Run runs passed |",
              "| :-- | :-- | --: | --: | --: |"]
    for key, r in out["rows"].items():
        group, action = key.split("|")
        lines.append(f"| {group} | `{action}` | {r['cases']} | {r['baseline']}/{r['runs']} | {r['run']}/{r['runs']} |")
    if out["not_gated"]:
        lines += ["", f"New since the baseline, not gated: {', '.join(out['not_gated'])}"]
    j = out["judge"]
    lines += ["", "**Reported, not gating yet** (provisional, PMAT-311):", "",
              f"- security misses (failing runs): {out['security_misses']} (proposal 0)",
              f"- failing style_only runs: {out['style_only_failures']} (proposal 0)"]
    for d in judge_report.DIMENSIONS:
        goal = f" (proposal >= {judge_report.PROPOSAL[d]})" if d in judge_report.PROPOSAL else ""
        lines.append(f"- judge mean {d}: {fmt(j['mean'][d])}{goal}")
    lines.append(f"- judged runs: {j['runs'] - j['not_judged'] - j['judge_errors']} of {j['runs']}, "
                 f"judge errors {j['judge_errors']}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The regression gate: compare an eval run with the baseline.")
    parser.add_argument("run_dir", type=Path, help="eval/runs/<run>, holding results.json and run.json")
    parser.add_argument("--baseline", type=Path, default=bl.BASELINE)
    parser.add_argument("--tolerance", type=int, default=TOLERANCE)
    parser.add_argument("--markdown", type=Path, help="also write the report to this file")
    args = parser.parse_args(argv)

    out = gate(json.loads(args.baseline.read_text()), json.loads((args.run_dir / "results.json").read_text()),
               json.loads((args.run_dir / "run.json").read_text()), gate_cases(), tolerance=args.tolerance)
    text = render(out)
    print(text)
    if args.markdown:
        args.markdown.write_text(text)
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
