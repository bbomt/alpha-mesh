#!/usr/bin/env python3
"""Eval case miner for review-pull-request (PMAT-300).

A standalone data generator: it runs `mesh review-pull-request` as a user would and reads the
report it prints. It never imports or changes the review code, and never writes to GitHub:
reviews run with --no-post-comment, and gh is only used to list PRs and read their comments.

  mine    For N recently merged PRs, turn every high/medium finding into an unlabelled case:
          from the review comments on the PR's final commit (merged unchanged, so the worker
          dismissed them) and from a fresh review. Writes a run report with the False Positive
          Rate of the fresh run.
  rerun   Review again the PRs of unlabelled cases, with the model that raised each case, and
          label the cases that do not reproduce `not_reproduced`.
  label   Record a judge's verdict on a case (false_positive / real_issue).
  report  Recompute a run report's labelled rates from the current case labels.

Cases: eval/cases/ (committed). Reports: eval/reports/ (gitignored).
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent.parent
CASES_DIR = EVAL_DIR / "cases"
REPORTS_DIR = EVAL_DIR / "reports"

SCORED = ("high", "medium")  # the agent's scale is high/medium/low; only these count as failures
LABELS = ("unlabelled", "false_positive", "real_issue", "not_reproduced")
LINE_TOLERANCE = 5  # a re-run finding this close to the case's line reproduces it

_TITLES = ("# AI Code Review", "# Partial pull request review")
_COMMIT = re.compile(r"^> \*\*Reviewed commit:\*\* `([0-9a-f]{40})`")
_MODEL = re.compile(r"^> \*\*Model:\*\* (`+) ?(.+?) ?\1$")
_SECTION = re.compile(r"^### \S+ (High|Medium|Low)$")
_FINDING = re.compile(r"^#### (`+) ?(.+?) ?\1(?: · line (\d+))?(?: · model: (`+) ?(.+?) ?\4)?$")


# ── GitHub and mesh (the only side effects; tests replace both) ─────────────

def run_gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def run_mesh(args: list[str]) -> str:
    """The wrapper merges the review's stderr into stdout (and tees it to the daily log)."""
    return subprocess.run(["mesh", "review-pull-request", *args], capture_output=True, text=True).stdout


def merged_prs(gh, repo: str, limit: int) -> list[dict]:
    return json.loads(gh(["pr", "list", "--repo", repo, "--state", "merged", "--limit", str(limit),
                          "--json", "number,title,baseRefOid,headRefOid,url"]))


def pr_comments(gh, repo: str, number: int) -> list[str]:
    out = gh(["api", f"repos/{repo}/issues/{number}/comments", "--paginate", "--jq", ".[].body | @json"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def review(mesh, url: str, model: str | None) -> dict | None:
    """A fresh review of the PR's head, never posted. None if no report came back."""
    args = [url, "--no-post-comment", "--force-rerun"] + (["--model", model] if model else [])
    out = mesh(args)
    parsed = parse_report(out)
    if parsed is None:
        print(f"  ERROR: no review report for {url}; last output:\n" + "\n".join(out.splitlines()[-15:]),
              file=sys.stderr)
    return parsed


# ── Report parsing ──────────────────────────────────────────────────────────

def parse_report(text: str) -> dict | None:
    """Findings, reviewed commit and model of a review report (as printed or posted).

    Returns None if the text holds no report. head_sha is None for reports that do not record
    their commit (the old pr-review-performance format).
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(_TITLES)), None)
    if start is None:
        return None
    lines = lines[start:]
    head_sha = model = None
    for line in lines[1:]:
        if line.strip() and not line.startswith(">"):
            break  # end of the header block
        if m := _COMMIT.match(line):
            head_sha = m.group(1)
        if m := _MODEL.match(line):
            model = m.group(2)

    findings, severity, current, field = [], None, None, None
    for line in lines[1:]:
        if line.startswith("## "):
            severity = current = None  # past the issues
        elif m := _SECTION.match(line):
            severity, current = m.group(1).lower(), None
        elif severity and (m := _FINDING.match(line)):
            current = {"file": m.group(2), "line": int(m.group(3)) if m.group(3) else None,
                       "severity": severity, "summary": "", "fix": None, "failure_scenario": None,
                       "model": m.group(5) or model}
            findings.append(current)
            field = None
        elif current is not None:
            if line.startswith("**Problem:** "):
                field, current["summary"] = "summary", line[len("**Problem:** "):]
            elif line.startswith("**Fix:** "):
                field, current["fix"] = "fix", line[len("**Fix:** "):]
            elif line.startswith("---"):
                current = field = None
            elif field:
                current[field] += "\n" + line
    for f in findings:
        f["summary"] = f["summary"].strip()
        f["fix"] = f["fix"].strip() if f["fix"] else None
    return {"head_sha": head_sha, "model": model, "findings": findings}


# ── Case files ──────────────────────────────────────────────────────────────

def new_case(repo: str, pr: dict, head_sha: str, source: str, finding: dict) -> dict:
    finding = {k: v for k, v in finding.items() if k != "model"}
    key = [repo, pr["number"], head_sha, source, finding["file"], finding["line"], finding["severity"],
           finding["summary"]]
    case_id = hashlib.sha1(json.dumps(key).encode()).hexdigest()[:12]
    return {"case_id": case_id, "repo": repo, "pr_number": pr["number"], "base_sha": pr["baseRefOid"],
            "head_sha": head_sha, "source": source, "model": None, "finding": finding,
            "label": "unlabelled", "label_by": None, "label_date": None, "label_reason": None,
            "rerun_model": None, "rerun_sha": None, "rerun_reproduced": None, "rerun_finding": None}


def case_path(cases_dir: Path, case: dict) -> Path:
    return cases_dir / f"{case['repo'].split('/')[-1]}-pr{case['pr_number']}-{case['case_id']}.json"


def write_case(cases_dir: Path, case: dict) -> None:
    cases_dir.mkdir(parents=True, exist_ok=True)
    case_path(cases_dir, case).write_text(json.dumps(case, indent=2, ensure_ascii=False) + "\n")


def load_cases(cases_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(Path(cases_dir).glob("*.json"))]


def add_case(cases_dir: Path, case: dict) -> None:
    """Write a newly mined case; a case already on disk keeps its labels."""
    if not case_path(cases_dir, case).exists():
        write_case(cases_dir, case)


# ── Commands ────────────────────────────────────────────────────────────────

def mine(repo: str, limit: int, model: str | None, *, prs: list[int] | None = None, gh=run_gh, mesh=run_mesh,
         cases_dir: Path = CASES_DIR, reports_dir: Path = REPORTS_DIR, today: date | None = None) -> dict:
    today = today or date.today()
    results, skipped, earlier = [], 0, 0
    for pr in merged_prs(gh, repo, limit):
        if prs and pr["number"] not in prs:
            continue
        print(f"PR #{pr['number']}: {pr['title']}", file=sys.stderr)
        for body in pr_comments(gh, repo, pr["number"]):
            parsed = parse_report(body) if body.startswith(_TITLES) else None
            if parsed is None:
                continue
            if parsed["head_sha"] is None:
                skipped += 1
                continue
            # Only the final commit was merged unchanged: its findings were dismissed by the worker.
            # Findings on earlier commits may have been fixed before the merge.
            if parsed["head_sha"] != pr["headRefOid"]:
                earlier += 1
                continue
            for f in parsed["findings"]:
                if f["severity"] in SCORED:
                    add_case(cases_dir, {**new_case(repo, pr, parsed["head_sha"], "pr_comment", f),
                                         "model": f["model"]})

        print(f"  reviewing {pr['url']} ...", file=sys.stderr)
        parsed = review(mesh, pr["url"], model)
        if parsed is None:
            results.append({"pr_number": pr["number"], "title": pr["title"], "error": True})
            continue
        scored = [f for f in parsed["findings"] if f["severity"] in SCORED]
        case_ids = []
        for f in scored:
            case = {**new_case(repo, pr, parsed["head_sha"] or pr["headRefOid"], "fresh_run", f),
                    "model": f["model"] or model}
            add_case(cases_dir, case)
            case_ids.append(case["case_id"])
        results.append({"pr_number": pr["number"], "title": pr["title"], "head_sha": pr["headRefOid"],
                        "model": parsed["model"] or model, "passed": not scored, "false_positives": len(scored),
                        "case_ids": case_ids, "error": False})
        print(f"  {'pass' if not scored else 'FAIL'}: {len(scored)} high/medium finding(s)", file=sys.stderr)

    tested = [r for r in results if not r["error"]]
    failed = [r for r in tested if not r["passed"]]
    run = {"repo": repo, "run_date": today.isoformat(),
           "model": model or next((r["model"] for r in tested if r["model"]), None),
           "prs": results, "comments_skipped_no_commit": skipped, "comments_skipped_earlier_commit": earlier,
           "totals": {"prs_tested": len(tested), "prs_failed": len(failed), "prs_errored": len(results) - len(tested),
                      "false_positive_rate": len(failed) / len(tested) if tested else None}}
    run["labelled"] = labelled_summary(run, load_cases(cases_dir))
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"eval_report_{today:%Y%m%d}.json"
    path.write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n")
    print(f"Report: {path}", file=sys.stderr)
    return run


def _reproduces(case_finding: dict, f: dict) -> bool:
    if f["severity"] not in SCORED or f["file"] != case_finding["file"]:
        return False
    a, b = case_finding["line"], f["line"]
    return a == b if a is None or b is None else abs(a - b) <= LINE_TOLERANCE


def rerun(*, cases_dir: Path = CASES_DIR, mesh=run_mesh, today: date | None = None,
          retry_not_reproduced: bool = False) -> None:
    today = today or date.today()
    groups: dict[tuple, list[dict]] = {}
    for c in load_cases(cases_dir):
        if (c["label"] == "unlabelled" and c["rerun_reproduced"] is None) or \
                (retry_not_reproduced and c["label"] == "not_reproduced"):
            groups.setdefault((c["repo"], c["pr_number"], c["model"]), []).append(c)
    for (repo, number, model), group in groups.items():
        url = f"https://github.com/{repo}/pull/{number}"
        print(f"Re-running {url} with {model or '(default model)'} for {len(group)} case(s)...", file=sys.stderr)
        parsed = review(mesh, url, model)
        if parsed is None:
            continue
        for c in group:
            match = next((f for f in parsed["findings"] if _reproduces(c["finding"], f)), None)
            c.update(rerun_model=model or parsed["model"], rerun_sha=parsed["head_sha"],
                     rerun_reproduced=match is not None, rerun_finding=match)
            if match is None:
                fin = c["finding"]
                reason = (f"Re-run at {(parsed['head_sha'] or '?')[:7]} had no high/medium finding in "
                          f"{fin['file']} within ±{LINE_TOLERANCE} lines of line {fin['line']}.")
                c.update(label="not_reproduced", label_by="eval_cases.py rerun", label_date=today.isoformat(),
                         label_reason=reason)
            elif c["label"] == "not_reproduced":  # reproduced on a retry: back to the judge
                c.update(label="unlabelled", label_by=None, label_date=None, label_reason=None)
            write_case(cases_dir, c)
        print(f"  {sum(c['rerun_reproduced'] for c in group)} of {len(group)} reproduced", file=sys.stderr)


def label(case_id: str, value: str, reason: str, *, cases_dir: Path = CASES_DIR, by: str = "claude-code",
          today: date | None = None) -> dict:
    if value not in LABELS:
        raise ValueError(f"label must be one of {', '.join(LABELS)}")
    [case] = [c for c in load_cases(cases_dir) if c["case_id"] == case_id] or [None]
    if case is None:
        raise ValueError(f"no case {case_id} in {cases_dir}")
    case.update(label=value, label_by=by, label_date=(today or date.today()).isoformat(), label_reason=reason)
    write_case(cases_dir, case)
    return case


def labelled_summary(run: dict, cases: list[dict]) -> dict:
    """Rates from judged cases only: a PR fails only for a fresh-run case labelled false_positive.

    unlabelled, not_reproduced and real_issue cases never make a PR fail here.
    """
    label_of = {c["case_id"]: c["label"] for c in cases}
    tested = [r for r in run["prs"] if not r["error"]]
    fp_prs = [r["pr_number"] for r in tested
              if any(label_of.get(i) == "false_positive" for i in r["case_ids"])]
    counts = {name: 0 for name in LABELS}
    for c in cases:
        if c["repo"] == run["repo"]:
            counts[c["label"]] += 1
    return {"prs_with_false_positive": fp_prs,
            "false_positive_rate": len(fp_prs) / len(tested) if tested else None,
            "label_counts": counts}


def report(report_path: Path, *, cases_dir: Path = CASES_DIR) -> dict:
    run = json.loads(Path(report_path).read_text())
    run["labelled"] = labelled_summary(run, load_cases(cases_dir))
    Path(report_path).write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n")
    return run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("mine", help="mine cases from merged PRs and score a fresh run")
    p.add_argument("--repo", required=True, help="owner/name, e.g. bbomt/alpha-mesh")
    p.add_argument("--limit", type=int, default=20, help="how many recently merged PRs (default 20)")
    p.add_argument("--model", help="review model (default: CODE_REVIEW_MODEL from .env)")
    p.add_argument("--prs", help="only these PR numbers, comma-separated")
    p = sub.add_parser("rerun", help="re-review unlabelled cases with the model that raised them")
    p.add_argument("--retry-not-reproduced", action="store_true", help="also retry cases labelled not_reproduced")
    p = sub.add_parser("label", help="record a verdict on a case")
    p.add_argument("case_id")
    p.add_argument("label", choices=LABELS)
    p.add_argument("--reason", required=True)
    p.add_argument("--by", default="claude-code")
    p = sub.add_parser("report", help="recompute a run report's labelled rates")
    p.add_argument("report_path")
    args = parser.parse_args(argv)

    if args.command == "mine":
        prs = [int(n) for n in args.prs.split(",")] if args.prs else None
        run = mine(args.repo, args.limit, args.model, prs=prs)
        print(json.dumps(run["totals"], indent=2))
    elif args.command == "rerun":
        rerun(retry_not_reproduced=args.retry_not_reproduced)
    elif args.command == "label":
        label(args.case_id, args.label, args.reason, by=args.by)
    else:
        print(json.dumps(report(args.report_path)["labelled"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
