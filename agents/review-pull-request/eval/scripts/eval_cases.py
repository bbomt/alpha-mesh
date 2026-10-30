#!/usr/bin/env python3
"""Eval cases for review-pull-request (PMAT-300, PMAT-301, PMAT-307, PMAT-309).

A standalone tool next to the review agent: it never imports or changes the review code, and
never writes to GitHub (gh is only used to read PRs, their comments and their commits).

  mine    Build the dataset from merged PRs, with no model calls. A high/medium finding from a
          review comment on the PR is labelled by what happened to its ID (R<PR>-<commit7>-<NNN>):
          - a real issue if a commit in the PR cites the ID: the worker fixed it;
          - a false positive if a PR comment dismisses it with the line
            `Dismissed review finding <ID>: <reason>` (a reason starting `duplicate of <ID>` gives
            no case: the original is the case);
          - no case if both happened (conflict), neither happened (unresolved) or it has no ID.
  label   Override a case's label (false_positive / real_issue) with a reason.

Each case also gets an `expected` block derived from its label (PMAT-309): the properties a good
review has, for the harness to check. A hand-edited `expected` or `notes` survives a rerun.

Cases: eval/cases/<repo name>/<ID>.json (committed); eval/cases/legacy-file-rule/ holds the frozen
cases of PMAT-300's file rule, which mine never reads or writes.
"""

import argparse
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
_FINDING_ID = re.compile(r"\bR\d+-[0-9a-f]{7}-\d{3}\b")
_DISMISSAL = re.compile(r"^Dismissed review finding (R(\d+)-[0-9a-f]{7}-\d{3}): (.+)$")
_DUPLICATE = re.compile(r"^duplicate of R\d+-[0-9a-f]{7}-\d{3}\b")


# ── GitHub, read only (the only side effect; tests replace it) ──────────────

def run_gh(args: list[str]) -> str:
    # A list and no shell: each value reaches gh as one argument and is never parsed by a shell.
    # Values that go into gh api paths are checked first (check_repo, int), so they cannot
    # point a read at another endpoint.
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def check_repo(repo: str) -> str:
    if not _REPO.match(repo):
        raise ValueError(f"repo must be owner/name, got {repo!r}")
    return repo


def merged_prs(gh, repo: str, limit: int) -> list[dict]:
    return json.loads(gh(["pr", "list", "--repo", repo, "--state", "merged", "--limit", str(limit),
                          "--json", "number,title,baseRefOid,headRefOid,url"]))


def pr_comments(gh, repo: str, number: int) -> list[str]:
    """Comment bodies, oldest first."""
    out = gh(["api", f"repos/{check_repo(repo)}/issues/{int(number)}/comments", "--paginate", "--jq", ".[].body | @json"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def pr_commits(gh, repo: str, number: int) -> list[dict]:
    """The PR's commits ({sha, message}), oldest first. GitHub lists at most 250."""
    out = gh(["api", f"repos/{check_repo(repo)}/pulls/{int(number)}/commits", "--paginate", "--jq",
              ".[] | {sha, message: .commit.message} | @json"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def cited_ids(commits: list[dict]) -> dict[str, str]:
    """Finding ID -> the first commit whose message cites it."""
    cited: dict[str, str] = {}
    for c in commits:
        for finding_id in _FINDING_ID.findall(c["message"]):
            cited.setdefault(finding_id, c["sha"])
    return cited


def dismissals(comments: list[str], number: int) -> dict[str, str]:
    """Finding ID of this PR -> the reason of the last PR comment line that dismisses it
    (a later line corrects an earlier one)."""
    dismissed: dict[str, str] = {}
    for body in comments:
        for line in body.splitlines():
            if (m := _DISMISSAL.match(line.rstrip())) and int(m.group(2)) == number:
                dismissed[m.group(1)] = m.group(3).strip()
    return dismissed


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

def repo_dir(cases_dir: Path, repo: str) -> Path:
    """One folder per repo: a finding ID is unique only within its repo (PR numbers are per repo)."""
    return Path(cases_dir) / repo.split("/")[-1]


def derive_expected(label: str, finding: dict) -> dict:
    """What a good review of the case's code does, from its label. A false positive only forbids
    raising it again; whether a new finding in the same file is "the same" is the judge's call."""
    if label == "real_issue":
        return {"must_flag": True, "flag_file": finding["file"], "min_severity": finding["severity"]}
    return {"must_not_reraise": True, "flag_file": finding["file"]}


def write_case(path: Path, case: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, indent=2, ensure_ascii=False) + "\n")


def case_files(cases_dir: Path) -> list[Path]:
    """Every case file: the per-repo folders and legacy-file-rule/."""
    return sorted(Path(cases_dir).glob("*/*.json"))


def load_cases(cases_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in case_files(cases_dir)]


# ── Commands ────────────────────────────────────────────────────────────────

def mine(repo: str, limit: int, *, prs: list[int] | None = None, gh=run_gh, cases_dir: Path = CASES_DIR,
         today: date | None = None) -> dict:
    check_repo(repo)  # before any gh call
    today = today or date.today()
    counts = {"cited": 0, "dismissed": 0, "duplicate": 0, "unresolved": 0, "conflict": 0, "no_id": 0}
    found: dict[str, dict] = {}
    seen: set[str] = set()
    mined = 0
    for pr in merged_prs(gh, repo, limit):
        if prs and pr["number"] not in prs:
            continue
        mined += 1
        cited = cited_ids(pr_commits(gh, repo, pr["number"]))
        comments = pr_comments(gh, repo, pr["number"])
        dismissed = dismissals(comments, pr["number"])
        for body in comments:
            parsed = parse_report(body) if body.startswith(_TITLES) else None
            for f in (parsed or {}).get("findings", []):
                if f["severity"] not in CASE_SEVERITIES:
                    continue
                # Only this PR's IDs: an ID names its PR, and is unique within it.
                finding_id = f["id"] if f["id"] and f["id"].startswith(f"R{pr['number']}-") else None
                if finding_id is None:
                    counts["no_id"] += 1
                    continue
                if finding_id in seen:  # the same report posted again
                    continue
                seen.add(finding_id)
                fix, reason = cited.get(finding_id), dismissed.get(finding_id)
                if fix and reason:
                    counts["conflict"] += 1  # no deterministic label
                    continue
                if reason and _DUPLICATE.match(reason):
                    counts["duplicate"] += 1  # the original ID is the case
                    continue
                if not fix and not reason:
                    counts["unresolved"] += 1
                    continue
                counts["cited" if fix else "dismissed"] += 1
                # Every case applies to raised_sha, the code the review saw; head_sha is the merged code.
                case = {
                    "case_id": finding_id, "repo": repo, "pr_number": pr["number"], "base_sha": pr["baseRefOid"],
                    "head_sha": pr["headRefOid"], "raised_sha": parsed["head_sha"], "fix_sha": fix,
                    "source": "pr_comment", "model": f["model"], "finding": {k: v for k, v in f.items() if k != "model"},
                    "label": "false_positive", "label_by": "rule", "label_date": today.isoformat(),
                    "label_reason": reason}
                if fix:
                    case.update(label="real_issue", label_reason=f"Commit {fix[:7]} in the PR cites finding {finding_id}.")
                found[finding_id] = case
        print(f"PR #{pr['number']}: {sum(c['pr_number'] == pr['number'] for c in found.values())} case(s)",
              file=sys.stderr)

    folder = repo_dir(cases_dir, repo)
    for case_id, case in found.items():
        path = folder / f"{case_id}.json"
        existing = json.loads(path.read_text()) if path.exists() else None
        if existing and existing["label_by"] != "rule":  # keep a judge's override
            case.update({k: existing[k] for k in ("label", "label_by", "label_date", "label_reason")})
        elif existing and (existing["label"], existing["label_reason"]) == (case["label"], case["label_reason"]):
            case["label_date"] = existing["label_date"]  # the label did not change: no date churn
        case["category"] = "mined"
        case["expected"] = derive_expected(case["label"], case["finding"])
        if existing and "expected" in existing and existing["expected"] != derive_expected(existing["label"],
                                                                                            existing["finding"]):
            case["expected"] = existing["expected"]  # differs from what the rule gives: edited by hand
        case["notes"] = existing.get("notes", "") if existing else ""
        if case != existing:
            write_case(path, case)
    return {"repo": repo, "run_date": today.isoformat(), "prs_mined": mined, "cases_written": len(found),
            "findings": counts}


def label(case_id: str, value: str, reason: str, *, cases_dir: Path = CASES_DIR, by: str = "claude-code",
          today: date | None = None) -> dict:
    if value not in LABELS:
        raise ValueError(f"label must be one of {', '.join(LABELS)}")
    matches = [(p, c) for p in case_files(cases_dir) if (c := json.loads(p.read_text()))["case_id"] == case_id]
    if not matches:
        raise ValueError(f"no case {case_id} in {cases_dir}")
    if len(matches) > 1:
        raise ValueError(f"case {case_id} is in several folders: {', '.join(str(p) for p, _ in matches)}")
    [(path, case)] = matches
    case.update(label=value, label_by=by, label_date=(today or date.today()).isoformat(), label_reason=reason,
                expected=derive_expected(value, case["finding"]))  # a new label makes a hand edit stale
    write_case(path, case)
    return case


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("mine", help="build cases from review comments, dismissals and PR commits (no model)")
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
