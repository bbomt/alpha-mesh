"""Check one PR's evals (PMAT-312): the logic of the required check `evals-gate`, and `mesh merge-pr`'s report.

    python3 eval/scripts/check_pr_evals.py https://github.com/<owner>/<repo>/pull/<n>   # exit 0: may merge
    python3 eval/scripts/check_pr_evals.py --evals-only <PR url>   # the evals items only (the job)

`mesh merge-pr` merges only when nothing in the report blocks, and prints it when it refuses. The workflow `.github/workflows/evals-gate.yml` runs it with `--evals-only` on every PR: GitHub then blocks
the merge itself (the check is required on master), whoever merges.

The report's first line is the verdict (READY TO MERGE, or BLOCKED by n items); then one line per item, OK or BLOCKED,
saying in words what it found; then, when blocked, the next command. The items, in order: the PR is open; it is up to
date with its base; and, when the PR changes a gated file (list_evals_gated_files.py), `evals` = success on its head
(posted by `mesh run-evals` on the dev box), or on an older commit of the PR with no gated file changed since. A PR
that changes no gated file needs no evals. Standard library only: `mesh merge-pr` runs it with the system python3.
"""

from __future__ import annotations  # the system python3 that mesh merge-pr uses may be older than 3.10

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import list_evals_gated_files  # noqa: E402

@dataclass
class Result:
    ok: bool
    text: str


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def _status(gh, repo: str, ref: str, context: str) -> tuple[str, str]:
    """(state, description) of the commit status <context> on <ref>; ("missing", "") when it has none."""
    statuses = json.loads(gh("api", f"repos/{repo}/commits/{ref}/status")).get("statuses", [])
    found = next((s for s in statuses if s["context"] == context), None)
    return (found["state"], found.get("description") or "") if found else ("missing", "")


def _evals_line(state: str, desc: str, head: str) -> str:
    at = f"head {head[:7]}"
    return {"success": f"evals passed on {at}",
            "failure": f"evals failed on {at}",
            "pending": f"evals are still running on {at} (or stopped without a verdict)",
            "error": f"evals stopped with an error on {at}",
            "missing": f"evals have not run on {at}"}.get(state, f"evals on {at}: {state}") + (f": {desc}" if desc else "")


def check(url: str, gh=_gh, evals_only: bool = False) -> Result:
    m = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"not a PR url: {url}")
    repo, number = m.groups()
    pr = json.loads(gh("pr", "view", url, "--json", "state,headRefOid,baseRefName"))
    head, base = pr["headRefOid"], pr["baseRefName"]
    items, nxt = [], None

    def item(passed: bool, text: str, detail=()):
        items.append((passed, text, detail))

    if not evals_only:  # GitHub checks these itself (the branch rule "up to date"); the job reports evals only
        is_open = pr["state"] == "OPEN"
        item(is_open, "the PR is open" if is_open else f"the PR is not open (it is {pr['state'].lower()})")
        behind = json.loads(gh("api", f"repos/{repo}/compare/{base}...{head}"))["behind_by"]
        item(behind == 0, f"it is up to date with {base}" if not behind else f"it is {behind} commit(s) behind {base}")
        if behind:
            nxt = f"merge {base} into the branch, rerun the tests, push"

    gated = list_evals_gated_files.triggers(gh("pr", "diff", url, "--name-only").split())
    if not gated:
        item(True, "it changes no agent file, so it needs no evals")
    else:
        item(True, f"it changes {len(gated)} agent file(s), so it needs a passing evals status", gated)
        state, desc = _status(gh, repo, head, "evals")
        carried = None if state == "success" else _carried(gh, repo, number, head)
        if carried:
            item(True, f"evals passed on {carried[:7]}, an older commit of this PR, and no agent file changed since "
                       f"(head {head[:7]}: {state})")
        else:
            item(state == "success", _evals_line(state, desc, head))
            if state != "success":
                nxt = nxt or f"mesh run-evals {url}"

    blocked = sum(not passed for passed, _, _ in items)
    verdict = "READY TO MERGE" if not blocked else f"BLOCKED by {blocked} item{'s' if blocked > 1 else ''}"
    lines = [f"Merge status for PR #{number} (head {head[:7]}): {verdict}", ""]
    for passed, text, detail in items:
        lines.append(f"  {'OK' if passed else 'BLOCKED':<8} {text}")
        lines += [f"           {d}" for d in detail]
    if nxt:
        lines += ["", f"Next: {nxt}"]
    return Result(not blocked, "\n".join(lines))


def _carried(gh, repo: str, number: str, head: str) -> str | None:
    """The newest older commit of the PR with evals = success, if no gated file changed between it and head."""
    commits = [c["sha"] for c in json.loads(gh("api", f"repos/{repo}/pulls/{number}/commits?per_page=100"))]
    for sha in reversed(commits[:-1] if commits and commits[-1] == head else commits):
        if _status(gh, repo, sha, "evals")[0] == "success":
            files = json.loads(gh("api", f"repos/{repo}/compare/{sha}...{head}")).get("files", [])
            changed = list_evals_gated_files.triggers(f["filename"] for f in files)
            if changed:
                return None  # a later commit changed a gated file: the old pass says nothing about it
            return sha
    return None


if __name__ == "__main__":
    args = sys.argv[1:]
    only = "--evals-only" in args
    args = [a for a in args if a != "--evals-only"]
    if len(args) != 1:
        sys.exit("usage: check_pr_evals.py [--evals-only] <PR url>")
    result = check(args[0], evals_only=only)
    print(result.text)
    sys.exit(0 if result.ok else 1)
