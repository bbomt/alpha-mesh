"""Build eval/baseline.json from one eval run (PMAT-304).

    uv run python eval/scripts/write_baseline.py eval/runs/<run>

The run folder holds Promptfoo's results.json and run.json (commit, model, date, k, window), both
written by run_eval.sh. Each stored review is re-scored against the case files as they are now
(case_rules.check, the same rule as the Promptfoo assert), so a label error fixed after the run
counts without reviewing again. Every failing case needs an entry in eval/error_analysis.json
({case_id: {failure_class, reason}}) before the baseline is written; harness errors are classed
automatically. Label changes since the run are listed as label_errors_fixed.

It also records the judge's means (PMAT-340), which the gate compares with. They need every repeat judged: a run
made with --judge-repeats all, or a judge run of its stored reviews passed as --judge-results:

    eval/scripts/run_eval.sh --stored eval/runs/<run> --judge-repeats all --run judge-<run>
    uv run python eval/scripts/write_baseline.py eval/runs/<run> --judge-results eval/runs/judge-<run>/results.json
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import jsonschema

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "promptfoo")]
import case_rules  # noqa: E402
import judge_report  # noqa: E402

EVAL = case_rules.EVAL
REPO_ROOT = EVAL.parents[2]
BASELINE = EVAL / "baseline.json"
ANALYSIS = EVAL / "error_analysis.json"
SCHEMA = json.loads((EVAL / "schema" / "baseline.schema.json").read_text())


class NotAnalysed(Exception):
    """Some failing cases have no failure class and reason yet."""


class NotJudged(Exception):
    """Some runs have no judgment and no judge error: the judge's means would not cover the whole run."""


def judge_means(results: dict, cases: list[dict]) -> dict:
    """The judge's means over the development cases' rows (judge_report.gate_means), refused unless every row was
    judged or failed with a judge error."""
    ids = {c["case_id"] for c in cases}
    rows = [r for r in results["results"]["results"] if r["vars"]["case_id"] in ids]
    out = judge_report.gate_means(judge_report.collect({"results": {"results": rows}}, cases))
    if out["not_judged"]:
        raise NotJudged(f"{out['not_judged']} of {out['runs']} runs were not judged: the baseline needs every repeat "
                        "judged (run_eval.sh --judge-repeats all, or --stored <run> --judge-repeats all and "
                        "--judge-results)")
    return out


def same_reviews(results: dict, judged: dict) -> None:
    """--judge-results must judge this run's own reviews (a --stored run replays them): every (case, repeat) holds
    the same review in both, apart from the `repeat` the replay adds (llm_rubric skips repeats by it)."""
    def review(row):
        output = (row.get("response") or {}).get("output")
        output = json.loads(output) if isinstance(output, str) else output
        return {k: v for k, v in output.items() if k != "repeat"} if isinstance(output, dict) else output

    def reviews(res):
        return {(r["vars"]["case_id"], r.get("repeatIndex") or 0): review(r) for r in res["results"]["results"]}
    run, other = reviews(results), reviews(judged)
    if differ := sorted(f"{c} repeat {i}" for c, i in run.keys() | other.keys() if run.get((c, i)) != other.get((c, i))):
        raise NotJudged(f"--judge-results judged other reviews than this run's ({len(differ)} differ, first "
                        f"{differ[0]}): judge this run's stored reviews (run_eval.sh --stored <run>)")


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


def category_group(case: dict) -> str:
    """The by_category row of a case: public cases share `logic` with synthetic ones, and mined cases stay a
    group by source (PMAT-321), so both get their own rows."""
    if case.get("source") == "public_pr":
        return "public"
    return f"mined:{case['category']}" if case.get("source") == "pr_comment" else case["category"]


def pass_rates(entries: dict, by_id: dict, group) -> dict:
    """"<group(case)>/<action>" -> counts over the scored cases, for the README's tables."""
    out: dict[str, dict] = {}
    for case_id, entry in sorted(entries.items()):
        if entry["status"] == "harness_error":
            continue
        row = out.setdefault(f"{group(by_id[case_id])}/{entry['action']}",
                             {"cases": 0, "runs_passed": 0, "runs": 0, "passed_all": 0})
        row["cases"] += 1
        row["runs_passed"] += entry["runs"].count("pass")
        row["runs"] += len(entry["runs"])
        row["passed_all"] += entry["status"] == "pass"
    return out


def build(results: dict, cases: list[dict], meta: dict, *, analysis: dict, label_changes: list[dict],
          judge: dict) -> dict:
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
            "by_category": pass_rates(entries, by_id, category_group),
            "by_severity": pass_rates(entries, by_id, lambda c: (c.get("finding") or {}).get("severity") or "none"),
        },
        "label_errors_fixed": fixed,
        "judge": judge,
        "cases": entries,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build eval/baseline.json from one eval run.")
    parser.add_argument("run_dir", type=Path, help="eval/runs/<run>, with results.json and run.json")
    parser.add_argument("--judge-results", type=Path,
                        help="results.json of run_eval.sh --stored <run> --judge-repeats all, when the run itself "
                             "did not judge every repeat")
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
    held = case_rules.holdout_ids()
    changes = [c for c in label_changes(meta["commit"], case_rules.CASES_DIR, REPO_ROOT) if c["case_id"] not in held]
    judged = json.loads(args.judge_results.read_text()) if args.judge_results else results
    try:
        if args.judge_results:
            same_reviews(results, judged)
        # run: the run the means come from (the judge run's folder with --judge-results), for provenance
        judge = {"run": (args.judge_results.resolve().parent if args.judge_results else args.run_dir.resolve()).name,
                 **judge_means(judged, case_rules.development_cases())}
        # run: the run folder's real name (eval/runs/<run> may link to ~/.mesh/eval-runs/<run>), which
        # archive_runs.py keeps in S3 while this baseline names it (PMAT-336)
        baseline = build(results, case_rules.development_cases(),
                         {"run": args.run_dir.resolve().name, **meta, "cases_commit": git("rev-parse", "HEAD")},
                         analysis=analysis, label_changes=changes, judge=judge)
        jsonschema.validate(baseline, SCHEMA)
    except (NotAnalysed, NotJudged) as e:
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
