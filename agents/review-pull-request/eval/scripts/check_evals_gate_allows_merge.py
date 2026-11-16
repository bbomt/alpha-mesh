"""The merge status report for one PR (PMAT-312): `mesh merge-pr` merges only when nothing in it blocks, and
`mesh merge-pr --status` prints it without merging.

    python3 eval/scripts/check_evals_gate_allows_merge.py https://github.com/<owner>/<repo>/pull/<n>   # exit 0: may merge

The report's first line is the verdict (READY TO MERGE, or BLOCKED by n items); then one line per item, OK or BLOCKED,
saying in words what it found; then, when blocked, the next command. The items, in order: the PR is open; it is up to
date with its base; master is not red (a `master-evals-gated` failure on it blocks every merge except the revert PR,
branch `eval-revert/...`, or a gated PR whose evals passed); and, when the PR changes a gated file
(list_evals_gated_files.py), `evals` = success on its head, or on an older commit of the PR with no gated file changed
since. Standard library only: `mesh merge-pr` runs it with the system python3.
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

REVERT_PREFIX = "eval-revert/"


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


def check(url: str, gh=_gh) -> Result:
    m = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"not a PR url: {url}")
    repo, number = m.groups()
    pr = json.loads(gh("pr", "view", url, "--json", "state,headRefOid,baseRefName,headRefName"))
    head, base, branch = pr["headRefOid"], pr["baseRefName"], pr.get("headRefName", "")
    items, nxt = [], None

    def item(passed: bool, text: str, detail=()):
        items.append((passed, text, detail))

    is_open = pr["state"] == "OPEN"
    item(is_open, "the PR is open" if is_open else f"the PR is not open (it is {pr['state'].lower()})")
    behind = json.loads(gh("api", f"repos/{repo}/compare/{base}...{head}"))["behind_by"]
    item(behind == 0, f"it is up to date with {base}" if not behind else f"it is {behind} commit(s) behind {base}")
    if behind:
        nxt = f"merge {base} into the branch, rerun the tests, push"

    gated = list_evals_gated_files.triggers(gh("pr", "diff", url, "--name-only").split())
    evals_items, evals_ok = [], True
    if not gated:
        evals_items.append((True, "it changes no agent file, so it needs no evals", ()))
    else:
        evals_items.append((True, f"it changes {len(gated)} agent file(s), so it needs a passing evals status",
                            gated))
        state, desc = _status(gh, repo, head, "evals")
        carried = None if state == "success" else _carried(gh, repo, number, head)
        if carried:
            evals_items.append((True, f"evals passed on {carried[:7]}, an older commit of this PR, and no agent file "
                                      f"changed since (head {head[:7]}: {state})", ()))
        else:
            evals_ok = state == "success"
            evals_items.append((evals_ok, _evals_line(state, desc, head), ()))

    # Red master: only a PR that recovers it may merge, the revert PR or a gated PR whose evals passed on this
    # up-to-date head (the evals then ran on the whole code, as find_ungated_merges.py counts it).
    audit, _ = _status(gh, repo, base, "master-evals-gated")
    red = audit in ("failure", "error")
    revert = branch.startswith(REVERT_PREFIX)
    recovers = revert or (bool(gated) and evals_ok)
    if not red:
        why = ("find-ungated-merges has posted no result on it yet" if audit == "missing"
               else f"master-evals-gated: {audit}")
        item(True, f"{base} is not red ({why})")
    elif revert:
        item(True, f"{base} is red, and this is the revert PR that recovers it")
    elif recovers:
        item(True, f"{base} is red, and this PR's passing evals recover it")
    else:
        item(False, f"{base} is red: an agent change landed without passing evals (master-evals-gated: {audit})")
        nxt = nxt or "mesh run-evals --master"
    for passed, text, detail in evals_items:
        item(passed, text, detail)
    if not evals_ok:
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
    result = check(sys.argv[1])
    print(result.text)
    sys.exit(0 if result.ok else 1)
