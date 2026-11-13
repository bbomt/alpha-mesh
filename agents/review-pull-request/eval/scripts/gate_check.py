"""The eval gate's checklist for one PR (PMAT-312): `mesh merge-pr` merges only when it passes, and
`mesh eval-gate --check` prints it with the next command.

    python3 eval/scripts/gate_check.py https://github.com/<owner>/<repo>/pull/<n>   # exit 0: the gate allows a merge

The checks, in order: the PR is open and up to date with its base; master is not red (an `eval-audit` failure on
it blocks every merge except the revert PR, branch `eval-revert/...`); and, when the PR changes a gated file
(gate_triggers.py), `evals` = success on its head, or on an older commit of the PR with no gated file changed since.
Standard library only: `mesh merge-pr` runs it with the system python3.
"""

from __future__ import annotations  # the system python3 that mesh merge-pr uses may be older than 3.10

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_triggers  # noqa: E402

REVERT_PREFIX = "eval-revert/"


@dataclass
class Result:
    ok: bool
    text: str


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def _state(gh, repo: str, ref: str, context: str) -> str:
    statuses = json.loads(gh("api", f"repos/{repo}/commits/{ref}/status")).get("statuses", [])
    return next((s["state"] for s in statuses if s["context"] == context), "missing")


def check(url: str, gh=_gh) -> Result:
    m = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if not m:
        raise ValueError(f"not a PR url: {url}")
    repo, number = m.groups()
    pr = json.loads(gh("pr", "view", url, "--json", "state,headRefOid,baseRefName,headRefName"))
    head, base, branch = pr["headRefOid"], pr["baseRefName"], pr.get("headRefName", "")
    lines, ok, nxt = [], True, None

    def item(passed: bool, text: str):
        nonlocal ok
        ok = ok and passed
        lines.append(f"[{'x' if passed else ' '}] {text}")

    item(pr["state"] == "OPEN", f"PR open ({pr['state'].lower()})")
    behind = json.loads(gh("api", f"repos/{repo}/compare/{base}...{head}"))["behind_by"]
    item(behind == 0, f"up to date with {base}" + (f": {behind} commit(s) behind" if behind else ""))
    if behind:
        nxt = f"merge {base} into the branch, rerun the tests, push"

    gated = gate_triggers.triggers(gh("pr", "diff", url, "--name-only").split())
    gate_lines, gate_ok = [], True
    if not gated:
        gate_lines.append((True, "changes no gated file: no eval needed"))
    else:
        gate_lines.append((True, "changes gated files: " + ", ".join(gated)))
        state = _state(gh, repo, head, "evals")
        carried = None if state == "success" else _carried(gh, repo, number, head)
        if state == "success":
            gate_lines.append((True, f"evals on head {head[:7]}: success"))
        elif carried:
            gate_lines.append((True, f"evals on head {head[:7]}: {state}, carried from {carried[:7]} "
                                     "(success there, no gated file changed since)"))
        else:
            gate_ok = False
            gate_lines.append((False, f"evals on head {head[:7]}: {state}"))

    # Red master: only a PR that recovers it may merge, the revert PR or a gated PR whose gate passed on this
    # up-to-date head (the gate then ran on the whole code, as eval_audit.py counts it).
    audit = _state(gh, repo, base, "eval-audit")
    red = audit in ("failure", "error")
    revert = branch.startswith(REVERT_PREFIX)
    recovers = revert or (bool(gated) and gate_ok)
    note = " (this is the revert PR)" if red and revert else " (this PR's passing gate recovers it)" if red and recovers else ""
    item(not red or recovers, f"{base} audit: {audit}{note}")
    if red and not recovers:
        nxt = nxt or "mesh eval-gate --master   (master holds an agent change merged without a passing gate)"
    for passed, text in gate_lines:
        item(passed, text)
    if not gate_ok:
        nxt = nxt or f"mesh eval-gate {url}"
    if nxt:
        lines.append(f"next: {nxt}")
    return Result(ok, "\n".join(lines))


def _carried(gh, repo: str, number: str, head: str) -> str | None:
    """The newest older commit of the PR with evals = success, if no gated file changed between it and head."""
    commits = [c["sha"] for c in json.loads(gh("api", f"repos/{repo}/pulls/{number}/commits?per_page=100"))]
    for sha in reversed(commits[:-1] if commits and commits[-1] == head else commits):
        if _state(gh, repo, sha, "evals") == "success":
            files = json.loads(gh("api", f"repos/{repo}/compare/{sha}...{head}")).get("files", [])
            changed = gate_triggers.triggers(f["filename"] for f in files)
            if changed:
                return None  # a later commit changed a gated file: the old pass says nothing about it
            return sha
    return None


if __name__ == "__main__":
    result = check(sys.argv[1])
    print(result.text)
    sys.exit(0 if result.ok else 1)
