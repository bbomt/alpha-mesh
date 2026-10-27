#!/usr/bin/env python3
"""Eval cases for review-pull-request (PMAT-300).

A standalone tool next to the review agent: it never imports or changes the review code, and
never writes to GitHub (gh is only used to read PRs, comments and commit comparisons).

  mine    Build the false-positive dataset from merged PRs, with no model calls. A case is a
          high/medium finding from a review comment on the PR, on a file that did not change
          between the commit that review covered and the PR's final commit: the worker saw it
          and merged that exact code anyway.
  label   Override a case's label (false_positive / real_issue) with a reason.
  score   Run a full `mesh review-pull-request --no-post-comment` on merged PRs and report the
          False Positive Rate: PRs with any high/medium finding / PRs reviewed.

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

SCORED = ("high", "medium")  # the agent's scale is high/medium/low; only these count
LABELS = ("false_positive", "real_issue")

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
    """Comment bodies, oldest first."""
    out = gh(["api", f"repos/{repo}/issues/{number}/comments", "--paginate", "--jq", ".[].body | @json"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def changed_files(gh, repo: str, base: str, head: str) -> set[str] | None:
    """Files changed from base to head (both names of a rename), or None if that cannot be told:
    base is not an ancestor of head (rewritten out of the PR's history) or the list is cut off."""
    out = json.loads(gh(["api", f"repos/{repo}/compare/{base}...{head}", "--jq",
                         "{status, truncated: ((.files | length) >= 300), "
                         "files: [.files[] | .filename, (.previous_filename // empty)]}"]))
    if out["status"] not in ("ahead", "identical") or out["truncated"]:
        return None
    return set(out["files"])


def review(mesh, url: str, model: str | None) -> dict | None:
    """A full review of the PR's final commit, never posted. None if no report came back."""
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

def case_path(cases_dir: Path, case: dict) -> Path:
    return cases_dir / f"{case['repo'].split('/')[-1]}-pr{case['pr_number']}-{case['case_id']}.json"


def write_case(cases_dir: Path, case: dict) -> None:
    cases_dir.mkdir(parents=True, exist_ok=True)
    case_path(cases_dir, case).write_text(json.dumps(case, indent=2, ensure_ascii=False) + "\n")


def load_cases(cases_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(Path(cases_dir).glob("*.json"))]


# ── Commands ────────────────────────────────────────────────────────────────

def mine(repo: str, limit: int, *, prs: list[int] | None = None, gh=run_gh, cases_dir: Path = CASES_DIR,
         today: date | None = None) -> dict:
    today = today or date.today()
    skipped = {"file_changed": 0, "no_commit": 0, "not_ancestor": 0}
    found: dict[str, dict] = {}
    mined = 0
    for pr in merged_prs(gh, repo, limit):
        if prs and pr["number"] not in prs:
            continue
        mined += 1
        final = pr["headRefOid"]
        for body in pr_comments(gh, repo, pr["number"]):
            parsed = parse_report(body) if body.startswith(_TITLES) else None
            scored = [f for f in (parsed or {}).get("findings", []) if f["severity"] in SCORED]
            if not scored:
                continue
            raised = parsed["head_sha"]
            if raised is None:
                skipped["no_commit"] += len(scored)
                continue
            changed = set() if raised == final else changed_files(gh, repo, raised, final)
            if changed is None:
                skipped["not_ancestor"] += len(scored)
                continue
            for f in scored:
                if f["file"] in changed:
                    skipped["file_changed"] += 1
                    continue
                # One case per finding: a later review repeating it (same line, maybe reworded) is the same case.
                key = [repo, pr["number"], f["file"], f["line"] if f["line"] is not None else f["summary"]]
                case_id = hashlib.sha1(json.dumps(key).encode()).hexdigest()[:12]
                if case_id in found:
                    found[case_id]["times_raised"] += 1
                    continue
                found[case_id] = {
                    "case_id": case_id, "repo": repo, "pr_number": pr["number"], "base_sha": pr["baseRefOid"],
                    "head_sha": final, "raised_sha": raised, "source": "pr_comment", "model": f["model"],
                    "finding": {k: v for k, v in f.items() if k != "model"}, "times_raised": 1,
                    "label": "false_positive", "label_by": "rule", "label_date": today.isoformat(),
                    "label_reason": "Raised in review and merged with the file unchanged."}
        print(f"PR #{pr['number']}: {sum(c['pr_number'] == pr['number'] for c in found.values())} case(s)",
              file=sys.stderr)

    existing = {c["case_id"]: c for c in load_cases(cases_dir)}
    for case_id, case in found.items():
        if case_id in existing:  # keep a judge's override
            case.update({k: existing[case_id][k] for k in ("label", "label_by", "label_date", "label_reason")})
        write_case(cases_dir, case)
    return {"repo": repo, "run_date": today.isoformat(), "prs_mined": mined, "cases_written": len(found),
            "skipped": skipped}


def label(case_id: str, value: str, reason: str, *, cases_dir: Path = CASES_DIR, by: str = "claude-code",
          today: date | None = None) -> dict:
    if value not in LABELS:
        raise ValueError(f"label must be one of {', '.join(LABELS)}")
    case = next((c for c in load_cases(cases_dir) if c["case_id"] == case_id), None)
    if case is None:
        raise ValueError(f"no case {case_id} in {cases_dir}")
    case.update(label=value, label_by=by, label_date=(today or date.today()).isoformat(), label_reason=reason)
    write_case(cases_dir, case)
    return case


def score(repo: str, limit: int, model: str | None, *, prs: list[int] | None = None, gh=run_gh, mesh=run_mesh,
          reports_dir: Path = REPORTS_DIR, today: date | None = None) -> dict:
    today = today or date.today()
    results = []
    for pr in merged_prs(gh, repo, limit):
        if prs and pr["number"] not in prs:
            continue
        print(f"PR #{pr['number']}: reviewing {pr['url']} ...", file=sys.stderr)
        parsed = review(mesh, pr["url"], model)
        if parsed is None:
            results.append({"pr_number": pr["number"], "title": pr["title"], "error": True})
            continue
        scored = [f for f in parsed["findings"] if f["severity"] in SCORED]
        results.append({"pr_number": pr["number"], "title": pr["title"], "head_sha": parsed["head_sha"],
                        "model": parsed["model"] or model, "passed": not scored, "false_positives": len(scored),
                        "findings": scored, "error": False})
        print(f"  {'pass' if not scored else 'FAIL'}: {len(scored)} high/medium finding(s)", file=sys.stderr)

    tested = [r for r in results if not r["error"]]
    failed = [r for r in tested if not r["passed"]]
    run = {"repo": repo, "run_date": today.isoformat(),
           "model": model or next((r["model"] for r in tested if r["model"]), None), "prs": results,
           "totals": {"prs_tested": len(tested), "prs_failed": len(failed), "prs_errored": len(results) - len(tested),
                      "false_positive_rate": len(failed) / len(tested) if tested else None}}
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"eval_report_{today:%Y%m%d}.json"
    path.write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n")
    print(f"Report: {path}", file=sys.stderr)
    return run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("mine", "build false-positive cases from review comments (no model)"),
                            ("score", "run full reviews and report the False Positive Rate")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--repo", required=True, help="owner/name, e.g. bbomt/alpha-mesh")
        p.add_argument("--limit", type=int, default=20, help="how many recently merged PRs (default 20)")
        p.add_argument("--prs", help="only these PR numbers, comma-separated")
        if name == "score":
            p.add_argument("--model", help="review model (default: CODE_REVIEW_MODEL from .env)")
    p = sub.add_parser("label", help="override a case's label")
    p.add_argument("case_id")
    p.add_argument("label", choices=LABELS)
    p.add_argument("--reason", required=True)
    p.add_argument("--by", default="claude-code")
    args = parser.parse_args(argv)

    prs = [int(n) for n in args.prs.split(",")] if getattr(args, "prs", None) else None
    if args.command == "mine":
        print(json.dumps(mine(args.repo, args.limit, prs=prs), indent=2))
    elif args.command == "score":
        print(json.dumps(score(args.repo, args.limit, args.model, prs=prs)["totals"], indent=2))
    else:
        label(args.case_id, args.label, args.reason, by=args.by)
    return 0


if __name__ == "__main__":
    sys.exit(main())
