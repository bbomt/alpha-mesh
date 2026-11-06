"""Report the hold-out's pass rate from a run's Promptfoo results (PMAT-318).

    uv run python eval/scripts/holdout_report.py eval/runs/<run>/results-holdout.json

Totals only, and never a case ID or a reason: a failing hold-out case stays unread until the set is
retired. A results file holding other cases too (an older run of every case) is fine: they are ignored.
"""

import argparse
import json
import sys
from pathlib import Path

import baseline as bl

case_rules = bl.case_rules


def report(results: dict, cases: list[dict]) -> str:
    scored = bl.runs(results, cases)
    actions = {c["case_id"]: c["expected"]["action"] for c in cases}
    finished = {i: [r["result"] for r in rs] for i, rs in scored.items() if bl.status(rs) != "harness_error"}

    def rate(kept):
        passed = sum(r == "pass" for rs in kept for r in rs)
        total = sum(len(rs) for rs in kept)
        return f"{passed}/{total} runs ({passed / total:.0%})" if total else "no runs"

    true_bugs = [rs for i, rs in finished.items() if actions[i] == "raise_true_bug"]
    clean = [rs for i, rs in finished.items() if actions[i] != "raise_true_bug"]
    all_passed = sum(all(r == "pass" for r in rs) for rs in finished.values())
    return (f"Hold-out: {len(scored)} cases, {len(scored) - len(finished)} left out by harness errors. "
            f"Pass rate {rate(list(finished.values()))}; {all_passed}/{len(finished)} cases passed every repeat; "
            f"true bugs raised {rate(true_bugs)}; false or clean cases passed {rate(clean)}. "
            "A coarse check for a large drop; failing cases are not listed, on purpose.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Report the hold-out's pass rate from a run's results.")
    parser.add_argument("results", type=Path, help="Promptfoo results file of the run")
    args = parser.parse_args(argv)
    print(report(json.loads(args.results.read_text()), case_rules.holdout_cases()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
