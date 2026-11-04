"""Build eval/baseline.json from one eval run (PMAT-304).

    uv run python eval/scripts/baseline.py eval/runs/<run>

The run folder holds Promptfoo's results.json and run.json (commit, model, date, k, window), both
written by run_eval.sh. Each stored review is re-scored against the case files as they are now
(case_rules.check, the same rule as the Promptfoo assert), so a label error fixed after the run
counts without reviewing again. Every failing case needs an entry in eval/error_analysis.json
({case_id: {failure_class, reason}}) before the baseline is written; harness errors are classed
automatically. Label changes since the run are listed as label_errors_fixed.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "promptfoo"))
import case_rules  # noqa: E402

EVAL = case_rules.EVAL
REPO_ROOT = EVAL.parents[2]
BASELINE = EVAL / "baseline.json"
ANALYSIS = EVAL / "error_analysis.json"
SCHEMA = json.loads((EVAL / "schema" / "baseline.schema.json").read_text())


class NotAnalysed(Exception):
    """Some failing cases have no failure class and reason yet."""


def runs(results: dict, cases: list[dict], window: int = case_rules.WINDOW) -> dict[str, list[dict]]:
    """case_id -> one {result, reason, file_level} per repeat, in repeat order: pass / fail / error."""
    by_id = {c["case_id"]: c for c in cases}
    out: dict[str, list[dict]] = {}
    for row in sorted(results["results"]["results"], key=lambda r: r.get("repeatIndex") or 0):
        case = by_id.get(row["vars"]["case_id"])
        if case is None:  # deleted since the run
            continue
        response = row.get("response") or {}
        output = response.get("output")
        if row.get("failureReason") == 2 or output is None:
            reason = row.get("error") or response.get("error") or "the provider returned no output"
            out.setdefault(case["case_id"], []).append({"result": "error", "reason": reason, "file_level": "error"})
            continue
        review = json.loads(output) if isinstance(output, str) else output
        passed, reason = case_rules.check(case, review, window)
        out.setdefault(case["case_id"], []).append({
            "result": "pass" if passed else "fail", "reason": reason,
            "file_level": "pass" if case_rules.check_file_level(case, review) else "fail"})
    return out


def status(case_runs: list[dict]) -> str:
    results = [r["result"] for r in case_runs]
    if "error" in results:
        return "harness_error"
    if all(r == "pass" for r in results):
        return "pass"
    return "stable_failure" if all(r == "fail" for r in results) else "flaky"


def label_changes(since: str, cases_dir: Path, repo_root: Path) -> list[dict]:
    """Every case whose label, expected block or finding line (the location it is matched at) changed since the
    commit `since` (committed or not)."""
    def git(*args):
        return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True)

    rel = Path(cases_dir).resolve().relative_to(Path(repo_root).resolve())
    diff = git("diff", "--name-only", "--end-of-options", since, "--", str(rel))
    if diff.returncode != 0:  # an unknown commit must not read as "no label changes"
        raise RuntimeError(f"git diff {since} failed: {diff.stderr.strip()}")
    changes = []
    for name in diff.stdout.split():
        before = git("show", f"{since}:{name}")
        if before.returncode != 0:  # a new case, not a label change
            continue
        old = json.loads(before.stdout)
        path = Path(repo_root) / name
        new = json.loads(path.read_text()) if path.exists() else None
        line = lambda c: (c.get("finding") or {}).get("line")  # noqa: E731
        if new is None:
            changes.append({"case_id": old["case_id"], "path": name, "old_label": old["label"], "new_label": None,
                            "old_expected": old.get("expected"), "new_expected": None, "old_line": line(old),
                            "new_line": None, "reason": "case deleted"})
        elif (old["label"], old.get("expected"), line(old)) != (new["label"], new.get("expected"), line(new)):
            reason = new.get("label_reason") if new["label"] != old["label"] else new.get("notes")
            changes.append({"case_id": new["case_id"], "path": name, "old_label": old["label"],
                            "new_label": new["label"], "old_expected": old.get("expected"),
                            "new_expected": new.get("expected"), "old_line": line(old), "new_line": line(new),
                            "reason": reason or ""})
    return changes


def build(results: dict, cases: list[dict], meta: dict, *, analysis: dict, label_changes: list[dict]) -> dict:
    by_id = {c["case_id"]: c for c in cases}
    entries, missing = {}, []
    for case_id, case_runs in sorted(runs(results, cases, meta["window"]).items()):
        case, st = by_id[case_id], status(case_runs)
        entry = {"runs": [r["result"] for r in case_runs], "file_level_runs": [r["file_level"] for r in case_runs],
                 "status": st, "failure_class": None, "reason": None,
                 "category": case["category"], "action": case["expected"]["action"]}
        if st == "harness_error":
            entry.update(failure_class="harness_error",
                         reason=next(r["reason"] for r in case_runs if r["result"] == "error"))
        elif st != "pass":
            if case_id in analysis:
                entry.update(failure_class=analysis[case_id]["failure_class"], reason=analysis[case_id]["reason"])
            else:
                missing.append(f"  {case_id} ({st}): {next(r['reason'] for r in case_runs if r['result'] == 'fail')}")
        entries[case_id] = entry
    if missing:
        raise NotAnalysed(f"{len(missing)} failing case(s) need a failure_class and reason in "
                          f"{ANALYSIS.name}:\n" + "\n".join(missing))

    scored = [e for e in entries.values() if e["status"] != "harness_error"]
    scored_runs = [r for e in scored for r in e["runs"]]
    file_level = [r for e in scored for r in e["file_level_runs"]]
    fixed = [{"case_id": c["case_id"], "reason": c["reason"]} for c in label_changes]
    labelled = {c["case_id"] for c in fixed} | {i for i, e in entries.items() if e["failure_class"] == "label_error"}
    return {
        **meta,
        "summary": {
            "cases": len(entries),
            "scored_cases": len(scored),
            "pass_rate": round(scored_runs.count("pass") / len(scored_runs), 4) if scored_runs else 0,
            "pass_k": round(sum(e["status"] == "pass" for e in scored) / len(scored), 4) if scored else 0,
            "file_level_pass_rate": round(file_level.count("pass") / len(file_level), 4) if file_level else 0,
            "by_class": {"model_failure": sum(e["failure_class"] == "model_failure" for e in entries.values()),
                         "label_error": len(labelled),
                         "harness_error": sum(e["failure_class"] == "harness_error" for e in entries.values())},
        },
        "label_errors_fixed": fixed,
        "cases": entries,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build eval/baseline.json from one eval run.")
    parser.add_argument("run_dir", type=Path, help="eval/runs/<run>, with results.json and run.json")
    args = parser.parse_args(argv)

    meta = json.loads((args.run_dir / "run.json").read_text())
    if meta.pop("dirty", False):
        print("ERROR: this run was made with uncommitted changes (run.json \"dirty\"), so its commit is not the "
              "code that ran. Commit, and run again.", file=sys.stderr)
        return 1
    def git(*a):
        done = subprocess.run(["git", "-C", str(REPO_ROOT), *a], capture_output=True, text=True)
        if done.returncode != 0:  # a failing git status must not read as "no uncommitted cases"
            raise RuntimeError(f"git {a[0]} failed: {done.stderr.strip()}")
        return done.stdout.strip()

    if git("status", "--porcelain", "--", str(case_rules.CASES_DIR)):
        print("ERROR: eval/cases has uncommitted changes. Commit each label fix on its own first:\n"
              "  fix[PMAT-XX]: fixed label error in eval case <ID> (<short reason>)", file=sys.stderr)
        return 1
    results = json.loads((args.run_dir / "results.json").read_text())
    analysis = json.loads(ANALYSIS.read_text()) if ANALYSIS.exists() else {}
    changes = label_changes(meta["commit"], case_rules.CASES_DIR, REPO_ROOT)
    try:
        baseline = build(results, case_rules.load_cases(), meta | {"cases_commit": git("rev-parse", "HEAD")},
                         analysis=analysis, label_changes=changes)
        jsonschema.validate(baseline, SCHEMA)
    except NotAnalysed as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except jsonschema.ValidationError as e:
        print(f"ERROR: the baseline fails its guard ({'/'.join(map(str, e.absolute_path))}): {e.message}",
              file=sys.stderr)
        return 1
    BASELINE.write_text(json.dumps(baseline, indent=2) + "\n")
    s = baseline["summary"]
    print(f"Wrote {BASELINE.relative_to(REPO_ROOT)}: pass rate {s['pass_rate']:.0%}, pass^{baseline['k']} "
          f"{s['pass_k']:.0%}; {s['by_class']}; label_errors_fixed {len(baseline['label_errors_fixed'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
