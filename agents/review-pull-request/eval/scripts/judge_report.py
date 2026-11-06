"""The judge's scores for one run (PMAT-311), against the starting proposal, which is provisional: the owner sets
the final thresholds after agent tuning.

    uv run python eval/scripts/judge_report.py eval/runs/<run>/results.json
    uv run python eval/scripts/judge_report.py eval/runs/<run>/results.json --write-scores   # eval/judge/judge_scores.json

Besides the means it gives a second pass rate per action: the line-window check (case_rules.check) asks "is a finding
near this line?", the judge asks "is it the same claim?".
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "promptfoo")]
import case_rules  # noqa: E402
import judge_sample  # noqa: E402

SCORES = case_rules.EVAL / "judge" / "judge_scores.json"
PROPOSAL = {"correctness": 4.0, "coverage": 3.8}  # provisional (PMAT-311 section 3); also zero security misses, style_only 100%
DIMENSIONS = ("correctness", "coverage", "actionability")


def judge_pass(case: dict, judgment: dict) -> bool | None:
    """The case's expected action by the judge's same-claim verdict; None for raise_nothing (no claim to compare)."""
    expected, verdict = case["expected"], judgment["same_claim"]["verdict"]
    if expected["action"] == "raise_nothing":
        return None
    if expected["action"] == "skip_false_bug":
        return verdict is False
    severity = judgment["same_claim"].get("finding_severity")
    return verdict is True and case_rules.RANK[severity] >= case_rules.RANK[expected["min_severity"]]


def _component(row: dict, name: str) -> dict:
    parts = (row.get("gradingResult") or {}).get("componentResults") or []
    return next((p for p in parts if name in str((p.get("assertion") or {}).get("value"))), {})


def collect(results: dict, cases: list[dict]) -> list[dict]:
    """One entry per stored row: its case, repeat, the judge's judgment or error, and the line-window result."""
    by_id, held, out = {c["case_id"]: c for c in cases}, case_rules.holdout_ids(), []
    for row in results["results"]["results"]:
        case_id = row["vars"]["case_id"]
        if case_id in held:
            raise ValueError(f"{case_id} is a hold-out case: the judge's results cover development cases only")
        if case_id not in by_id:
            continue
        meta = _component(row, "llm_rubric").get("metadata") or {}
        out.append({"case": by_id[case_id], "repeat": row.get("repeatIndex") or 0, "judgment": meta.get("judgment"),
                    "error": meta.get("judge_error"), "line_pass": _component(row, "case_expectations").get("pass")})
    return out


def _mean(values: list) -> float | None:
    return sum(values) / len(values) if values else None


def summarise(collected: list[dict]) -> dict:
    judged = [e for e in collected if e["judgment"]]
    actions: dict[str, dict] = {}
    for e in judged:
        action = e["case"]["expected"]["action"]
        row = actions.setdefault(action, {"runs": 0, "line_pass": 0, "judge_pass": 0})
        row["runs"] += 1
        row["line_pass"] += bool(e["line_pass"])
        row["judge_pass"] += bool(judge_pass(e["case"], e["judgment"]))
    style = [e for e in judged if e["case"]["category"] == "style_only"]
    return {
        "runs": len(collected),
        "judge_errors": sum(bool(e["error"]) for e in collected),
        "mean": {d: _mean([e["judgment"][d]["score"] for e in judged if e["judgment"][d]["score"] is not None])
                 for d in DIMENSIONS},
        "by_group": {g: {d: _mean([e["judgment"][d]["score"] for e in judged
                                   if judge_sample.group(e["case"]) == g and e["judgment"][d]["score"] is not None])
                         for d in DIMENSIONS} for g in sorted({judge_sample.group(e["case"]) for e in judged})},
        "actions": actions,
        "security_misses": sum(e["case"]["category"] == "security" and judge_pass(e["case"], e["judgment"]) is False
                               for e in judged),
        "style_only_pass_rate": _mean([bool(e["line_pass"]) for e in style]),
        "models": Counter(e["judgment"].get("model_reported") or e["judgment"].get("model_requested") for e in judged),
        "providers": Counter(e["judgment"].get("provider") or "unknown" for e in judged),
        "cost": sum(e["judgment"].get("cost") or 0 for e in judged),
        "tokens": sum((e["judgment"].get("prompt_tokens") or 0) + (e["judgment"].get("completion_tokens") or 0)
                      for e in judged),
    }


def print_summary(s: dict) -> None:
    fmt = lambda v: "n/a" if v is None else f"{v:.2f}"  # noqa: E731
    print(f"Judge: {dict(s['models'])}; providers {dict(s['providers'])}")
    print(f"Runs: {s['runs']}, judge errors: {s['judge_errors']}, cost ${s['cost']:.4f}, {s['tokens']} tokens")
    print("Means, against the provisional proposal:")
    for d in DIMENSIONS:
        goal = f" (proposal >= {PROPOSAL[d]})" if d in PROPOSAL else ""
        print(f"  {d:14} {fmt(s['mean'][d])}{goal}")
    print(f"  security misses {s['security_misses']} (proposal 0)")
    rate = s["style_only_pass_rate"]
    print(f"  style_only pass rate {'n/a' if rate is None else format(rate, '.0%')} (proposal 100%)")
    print("By group (correctness / coverage / actionability):")
    for group, row in s["by_group"].items():
        print(f"  {group:22} " + " / ".join(fmt(row[d]) for d in DIMENSIONS))
    print("Pass rate per action, line window vs same claim:")
    for action, row in sorted(s["actions"].items()):
        claim = "n/a" if action == "raise_nothing" else f"{row['judge_pass']}/{row['runs']}"
        print(f"  {action:16} line window {row['line_pass']}/{row['runs']}, same claim {claim}")


def sample_scores(collected: list[dict], sample: list[str]) -> dict:
    """The judge's scores for the hand-scored sample (repeat 0 of each), for judge_agreement.py."""
    first = {e["case"]["case_id"]: e["judgment"] for e in collected if e["repeat"] == judge_sample.REPEAT and e["judgment"]}
    if missing := [c for c in sample if c not in first]:
        raise ValueError(", ".join(f"no judgment for {c}" for c in missing))
    any_judgment = first[sample[0]]
    return {"model": any_judgment["model_requested"], "rubric": any_judgment["rubric"],
            "cases": {c: {**{d: first[c][d]["score"] for d in DIMENSIONS}, "same_claim": first[c]["same_claim"]["verdict"]}
                      for c in sample}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The judge's scores for one run.")
    parser.add_argument("results", type=Path)
    parser.add_argument("--write-scores", action="store_true", help=f"write the sample's scores to {SCORES.name}")
    args = parser.parse_args(argv)
    collected = collect(json.loads(args.results.read_text()), case_rules.development_cases())
    print_summary(summarise(collected))
    if args.write_scores:
        sample = json.loads(judge_sample.SAMPLE.read_text())["cases"]
        SCORES.write_text(json.dumps(sample_scores(collected, sample), indent=2) + "\n")
        print(f"Wrote {SCORES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
