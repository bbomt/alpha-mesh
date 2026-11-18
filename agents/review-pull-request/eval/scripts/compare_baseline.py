"""Compare an eval run with eval/baseline.json (PMAT-304; PMAT-312's gate reuses it).

    uv run python eval/scripts/compare_baseline.py eval/runs/<run>/results.json

Lists new failures (each needs error analysis), new passes (an improvement, or flaky), unchanged
cases, cases the run could not score, and every label change since the baseline's cases_commit,
so a reviewer sees each one. Exits 1 when there is a new failure.
"""

import argparse
import json
import sys
from pathlib import Path

import write_baseline as bl

case_rules = bl.case_rules
SIMPLE = {"pass": "pass", "flaky": "fail", "stable_failure": "fail", "harness_error": "error"}


def compare(old: dict, results: dict, *, cases_dir: Path, repo_root: Path) -> dict:
    cases = case_rules.development_cases(cases_dir)
    report = {"new_failures": [], "new_passes": [], "unchanged": [], "errors": [],
              "label_changes": bl.label_changes(old["cases_commit"], cases_dir, repo_root)}
    for case_id, case_runs in sorted(bl.runs(results, cases, old.get("window", case_rules.WINDOW)).items()):
        now = SIMPLE[bl.status(case_runs)]
        before = SIMPLE.get(old["cases"].get(case_id, {}).get("status"))
        if now == "error":
            report["errors"].append(case_id)
        elif now == "fail" and before != "fail":
            report["new_failures"].append(case_id)
        elif now == "pass" and before == "fail":
            report["new_passes"].append(case_id)
        else:
            report["unchanged"].append(case_id)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare an eval run with the baseline.")
    parser.add_argument("results", type=Path, help="Promptfoo results.json of the run")
    parser.add_argument("--baseline", type=Path, default=bl.BASELINE)
    args = parser.parse_args(argv)

    report = compare(json.loads(args.baseline.read_text()), json.loads(args.results.read_text()),
                     cases_dir=case_rules.CASES_DIR, repo_root=bl.REPO_ROOT)
    for key in ("new_failures", "new_passes", "errors"):
        print(f"{key} ({len(report[key])}): {', '.join(report[key]) or '-'}")
    print(f"unchanged: {len(report['unchanged'])}")
    print(f"label_changes ({len(report['label_changes'])}):")
    for c in report["label_changes"]:
        print(f"  {c['case_id']}: {c['old_label']} -> {c['new_label']}; reason: {c['reason'] or '(none)'}")
    return 1 if report["new_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
