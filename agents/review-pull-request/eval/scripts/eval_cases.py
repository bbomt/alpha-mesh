#!/usr/bin/env python3
"""Eval cases for review-pull-request (PMAT-300).

A standalone tool next to the review agent: it never imports or changes the review code, and
never writes to GitHub (gh is only used to read PRs, comments and commit comparisons).

  mine    Build the false-positive dataset from merged PRs, with no model calls. A case is a
          high/medium finding from a review comment on the PR, on a file that did not change
          between the commit that review covered and the PR's final commit: the worker saw it
          and merged that exact code anyway.
  label   Override a case's label (false_positive / real_issue) with a reason.

Cases: eval/cases/ (committed).
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

CASE_SEVERITIES = ("high", "medium")  # only these become cases; low findings are often left by design
LABELS = ("false_positive", "real_issue")

_TITLES = ("# AI Code Review", "# Partial pull request review")
_COMMIT = re.compile(r"^> \*\*Reviewed commit:\*\* `([0-9a-f]{40})`")
_MODEL = re.compile(r"^> \*\*Model:\*\* (`+) ?(.+?) ?\1$")
_SECTION = re.compile(r"^### \S+ (High|Medium|Low)$")
_FINDING = re.compile(r"^#### (?:(R\d+-[0-9a-f]{7}-\d{3}) · )?(`+) ?(.+?) ?\2(?: · line (\d+))?(?: · model: (`+) ?(.+?) ?\5)?$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}$)[A-Za-z0-9_.][A-Za-z0-9_.-]*$")  # GitHub owner/name
_SHA = re.compile(r"^[0-9a-f]{40}$")


# ── GitHub, read only (the only side effect; tests replace it) ──────────────

def run_gh(args: list[str]) -> str:
    # A list and no shell: each value reaches gh as one argument and is never parsed by a shell.
    # Values that go into gh api paths are checked first (check_repo, check_sha), so they cannot
    # point a read at another endpoint.
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def check_repo(repo: str) -> str:
    if not _REPO.match(repo):
        raise ValueError(f"repo must be owner/name, got {repo!r}")
    return repo


def check_sha(sha: str) -> str:
    if not isinstance(sha, str) or not _SHA.match(sha):
        raise ValueError(f"not a full commit SHA: {sha!r}")
    return sha


def merged_prs(gh, repo: str, limit: int) -> list[dict]:
    return json.loads(gh(["pr", "list", "--repo", repo, "--state", "merged", "--limit", str(limit),
                          "--json", "number,title,baseRefOid,headRefOid,url"]))


def pr_comments(gh, repo: str, number: int) -> list[str]:
    """Comment bodies, oldest first."""
    out = gh(["api", f"repos/{check_repo(repo)}/issues/{int(number)}/comments", "--paginate", "--jq", ".[].body | @json"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def changed_files(gh, repo: str, base: str, head: str) -> set[str] | None:
    """Files changed from base to head (both names of a rename), or None if that cannot be told:
    base is not an ancestor of head (rewritten out of the PR's history) or the list is cut off."""
    out = json.loads(gh(["api", f"repos/{check_repo(repo)}/compare/{check_sha(base)}...{check_sha(head)}", "--jq",
                         "{status, truncated: ((.files | length) >= 300), "
                         "files: [.files[] | .filename, (.previous_filename // empty)]}"]))
    if out["status"] not in ("ahead", "identical") or out["truncated"]:
        return None
    return set(out["files"])


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
            current = {"file": m.group(3), "line": int(m.group(4)) if m.group(4) else None,
                       "severity": severity, "summary": "", "fix": None, "failure_scenario": None,
                       "model": m.group(6) or model, "id": m.group(1)}
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
    check_repo(repo)  # before any gh call
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
            candidates = [f for f in (parsed or {}).get("findings", []) if f["severity"] in CASE_SEVERITIES]
            if not candidates:
                continue
            raised = parsed["head_sha"]
            if raised is None:
                skipped["no_commit"] += len(candidates)
                continue
            changed = set() if raised == final else changed_files(gh, repo, raised, final)
            if changed is None:
                skipped["not_ancestor"] += len(candidates)
                continue
            for f in candidates:
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("mine", help="build false-positive cases from review comments (no model)")
    p.add_argument("--repo", required=True, help="owner/name, e.g. bbomt/alpha-mesh")
    p.add_argument("--limit", type=int, default=20, help="how many recently merged PRs (default 20)")
    p.add_argument("--prs", help="only these PR numbers, comma-separated")
    p = sub.add_parser("label", help="override a case's label")
    p.add_argument("case_id")
    p.add_argument("label", choices=LABELS)
    p.add_argument("--reason", required=True)
    p.add_argument("--by", default="claude-code")
    args = parser.parse_args(argv)

    if args.command == "mine":
        prs = [int(n) for n in args.prs.split(",")] if args.prs else None
        print(json.dumps(mine(args.repo, args.limit, prs=prs), indent=2))
    else:
        label(args.case_id, args.label, args.reason, by=args.by)
    return 0


if __name__ == "__main__":
    sys.exit(main())
