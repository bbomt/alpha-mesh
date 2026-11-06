"""Judge–human agreement (PMAT-311): how far the judge's scores can be trusted, per dimension.

    uv run python eval/scripts/judge_agreement.py eval/judge/human_labels.json eval/judge/judge_scores.json

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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Judge–human agreement, per dimension.")
    parser.add_argument("labels", type=Path)
    parser.add_argument("scores", type=Path)
    args = parser.parse_args(argv)
    human = json.loads(args.labels.read_text())["cases"]
    scored = json.loads(args.scores.read_text())
    if bad := _unfinished(human):
        print("ERROR: unfinished labels:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    if missing := [c for c in human if c not in scored["cases"]]:
        print("ERROR: " + ", ".join(f"no judge score for {c}" for c in missing), file=sys.stderr)
        return 1
    result = agreement(human, scored["cases"])
    print(f"Judge–human agreement: {len(human)} cases, judge model {scored['model']}, rubric {scored['rubric']}")
    for dimension in DIMENSIONS:
        row = result[dimension]
        within = "n/a" if row["within_1"] is None else f"{row['within_1']:.0%}"
        k = "undefined" if row["kappa"] is None else f"{row['kappa']:.2f}"
        print(f"  {dimension:14} within ±1: {within:>4}   kappa: {k:>9}   (n={row['n']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
