"""Judge agreement (PMAT-311): how far the judge's scores can be trusted, per dimension, against the reference labels.

    uv run python eval/scripts/judge_agreement.py --silver-calibration eval/judge/silver-ai-calibration.json \
        --gold-calibration eval/judge/gold-human-calibration.json eval/judge/judge_scores.json

The reference (PMAT-326) is built per case and dimension, never written: the human (gold) score where one is set,
else the silver labeller's. A `TODO` in the gold file is ignored, so with no human scores the reference is all silver.
With --gold-calibration alone, every gold score must be set. Silver-vs-gold discrepancies over 1 point are listed:
they show where silver was wrong.

Prints agreement within ±1 point and Cohen's kappa on pass/fail (score >= 4 is a pass). ±1 is lenient, and two raters
that pass nearly everything agree by chance; kappa corrects for that. A null score (no findings to grade) is left out.
"""

import argparse
import json
import sys
from pathlib import Path

DIMENSIONS = ("correctness", "coverage", "actionability")
PASS = 4


def kappa(pairs: list[tuple[int, int]]) -> float | None:
    """Cohen's kappa on pass/fail; None when chance agreement is total (one class only), where it is undefined."""
    if not pairs:
        return None
    n = len(pairs)
    a, b = [x >= PASS for x, _ in pairs], [y >= PASS for _, y in pairs]
    observed = sum(x == y for x, y in zip(a, b)) / n
    chance = (sum(a) / n) * (sum(b) / n) + (1 - sum(a) / n) * (1 - sum(b) / n)
    return None if chance == 1 else (observed - chance) / (1 - chance)


def agreement(human: dict, judge: dict) -> dict:
    out = {}
    for dimension in DIMENSIONS:
        pairs = [(human[c][dimension], judge[c][dimension]) for c in human
                 if human[c][dimension] is not None and judge[c][dimension] is not None]
        out[dimension] = {"n": len(pairs), "kappa": kappa(pairs),
                          "within_1": sum(abs(x - y) <= 1 for x, y in pairs) / len(pairs) if pairs else None}
    return out


def _unfinished(labels: dict) -> list[str]:
    bad = []
    for case_id, label in labels.items():
        bad += [f"{case_id}: {d}" for d in DIMENSIONS if label.get(d, "missing") not in (None, 1, 2, 3, 4, 5)]
    return bad


def _scored(value) -> bool:
    return not isinstance(value, bool) and value in (1, 2, 3, 4, 5)


def reference(silver: dict | None, gold: dict | None) -> tuple[dict, dict, list[tuple]]:
    """(the reference labels, {dimension: {"gold": n, "silver": n}}, the discrepancies over 1 point).
    Per case and dimension: the gold score if set, else silver. Inputs are the files' "cases"."""
    base = silver if silver is not None else gold
    labels, sources, discrepancies = {}, {d: {"gold": 0, "silver": 0} for d in DIMENSIONS}, []
    for case_id, label in base.items():
        labels[case_id] = {}
        for d in DIMENSIONS:
            human = (gold or {}).get(case_id, {}).get(d)
            machine = (silver or {}).get(case_id, {}).get(d)
            value, source = (human, "gold") if _scored(human) else (machine, "silver")
            labels[case_id][d] = value
            if value is not None:
                sources[d][source] += 1
            if _scored(human) and _scored(machine) and abs(human - machine) > 1:
                discrepancies.append((case_id, d, machine, human))
    return labels, sources, discrepancies


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Judge agreement with the reference labels, per dimension.")
    parser.add_argument("scores", type=Path, help="the judge's scores")
    parser.add_argument("--silver-calibration", type=Path, help="the silver labeller's scores")
    parser.add_argument("--gold-calibration", type=Path, help="the human scores; a set score overrides silver's")
    args = parser.parse_args(argv)
    if not (args.silver_calibration or args.gold_calibration):
        parser.error("give --silver-calibration, --gold-calibration or both")
    silver = json.loads(args.silver_calibration.read_text()) if args.silver_calibration else None
    gold = json.loads(args.gold_calibration.read_text())["cases"] if args.gold_calibration else None
    scored = json.loads(args.scores.read_text())
    if bad := _unfinished(silver["cases"] if silver else gold):
        print(f"ERROR: unfinished {'silver' if silver else 'gold'} labels:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    if silver and gold and (extra := sorted(set(gold) - set(silver["cases"]))):
        print("ERROR: " + ", ".join(f"no silver label for gold case {c}" for c in extra), file=sys.stderr)
        return 1
    labels, sources, discrepancies = reference(silver and silver["cases"], gold)
    if missing := [c for c in labels if c not in scored["cases"]]:
        print("ERROR: " + ", ".join(f"no judge score for {c}" for c in missing), file=sys.stderr)
        return 1
    result = agreement(labels, scored["cases"])
    print(f"Judge agreement: {len(labels)} cases, judge model {scored['model']}, rubric {scored['rubric']}")
    if silver:
        print(f"Reference: gold where set, else silver ({silver['labeller']['requested']}, rubric {silver['rubric']})")
    for dimension in DIMENSIONS:
        row = result[dimension]
        within = "n/a" if row["within_1"] is None else f"{row['within_1']:.0%}"
        k = "undefined" if row["kappa"] is None else f"{row['kappa']:.2f}"
        src = sources[dimension]
        print(f"  {dimension:14} within ±1: {within:>4}   kappa: {k:>9}   (n={row['n']}; "
              f"{dimension}: {src['gold']} gold, {src['silver']} silver)")
    if discrepancies:
        print("Silver vs gold, over 1 point:")
        for case_id, d, machine, human in discrepancies:
            print(f"  {case_id} {d}: silver {machine}, gold {human}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
